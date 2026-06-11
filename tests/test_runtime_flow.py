from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from agentic_mesh.artifacts import ArtifactStore
from agentic_mesh.config import load_mesh_config
from agentic_mesh.human_gates import FileHumanGateRequestStore
from agentic_mesh.journal import EventJournal
from agentic_mesh.messaging import MESSAGE_TYPE_DIRECT_CONVERSATION_REQUESTED
from agentic_mesh.messaging import MESSAGE_TYPE_SPONSOR_DIRECTIVE_REQUESTED
from agentic_mesh.models import AgentRunResult
from agentic_mesh.models import DocumentUpdate
from agentic_mesh.models import FlowState
from agentic_mesh.models import Handoff
from agentic_mesh.models import Message
from agentic_mesh.models import NotificationEventOverrideConfig
from agentic_mesh.models import QueueProposal
from agentic_mesh.models import RoleInstanceConfig
from agentic_mesh.models import RouteRequest
from agentic_mesh.models import WorkItemAction
from agentic_mesh.notifications import FileSourceRouteStore
from agentic_mesh.notifications import SourceRouteRecord
from agentic_mesh.problem_status import ProblemStatusStore
from agentic_mesh.problem_status import role_problem_status
from agentic_mesh.storage import FileConnectorOutbox
from agentic_mesh.runtime import AgentRuntime
from agentic_mesh.storage import FileMessageStore
from agentic_mesh.workers import StubCodexWorkerAdapter
from agentic_mesh.workers import WorkerAdapter
from agentic_mesh.work_queue import FileWorkQueueStore
from agentic_mesh.work_queue import SourceAnchor


class IncompleteHandoffWorker(WorkerAdapter):
    def run(
        self,
        instance: RoleInstanceConfig,
        message: Message,
        flow_state: FlowState,
    ) -> AgentRunResult:
        return AgentRunResult(
            status="completed",
            message="Completed with a minimal worker-emitted handoff.",
            document_updates=[
                DocumentUpdate(
                    path=flow_state.artifact_path,
                    content="Completed business framing.",
                )
            ],
            handoffs=[
                Handoff(
                    target_role="product-manager",
                    message_type="sdlc.product_definition",
                    payload={
                        "summary": "Ready for product definition.",
                    },
                )
            ],
        )


class StateMatchedHandoffWorker(WorkerAdapter):
    def run(
        self,
        instance: RoleInstanceConfig,
        message: Message,
        flow_state: FlowState,
    ) -> AgentRunResult:
        return AgentRunResult(
            status="completed",
            message="Completed with a state-matched configured handoff.",
            document_updates=[
                DocumentUpdate(
                    path=flow_state.artifact_path,
                    content="Completed business framing.",
                )
            ],
            handoffs=[
                Handoff(
                    target_role="product-manager",
                    message_type="sdlc.generic_handoff",
                    payload={
                        "summary": "Ready for product definition.",
                        "lifecycle_state": "product_definition",
                    },
                )
            ],
        )


class OutOfFlowHandoffWorker(WorkerAdapter):
    def run(
        self,
        instance: RoleInstanceConfig,
        message: Message,
        flow_state: FlowState,
    ) -> AgentRunResult:
        return AgentRunResult(
            status="completed",
            message="Completed with a warranted out-of-flow handoff.",
            document_updates=[
                DocumentUpdate(
                    path=flow_state.artifact_path,
                    content="Identified an implementation-ready repair.",
                )
            ],
            handoffs=[
                Handoff(
                    target_role="engineering",
                    message_type="sdlc.implementation",
                    payload={
                        "summary": "Runtime repair is already scoped.",
                        "lifecycle_state": "implementation",
                        "out_of_flow_reason": (
                            "The issue is a contained runtime defect with "
                            "clear acceptance criteria and no product ambiguity."
                        ),
                    },
                )
            ],
        )


class CorrectionRouteWorker(WorkerAdapter):
    def run(
        self,
        instance: RoleInstanceConfig,
        message: Message,
        flow_state: FlowState,
    ) -> AgentRunResult:
        return AgentRunResult(
            status="completed",
            message="Route correction back to Engineering.",
            routes=[
                RouteRequest(
                    target_role="engineering",
                    message_type="sdlc.consult.implementation",
                    payload={
                        "lifecycle_state": "implementation",
                        "review_status": "changes_requested",
                        "correction_status": "correction_requested",
                        "defect_id": "DEF-QCHR-QA-0001",
                        "gate_id": "qa_owner_review",
                        "review_artifact_path": "work-items/work-qchr/110-quality-evidence.md",
                        "required_change": "Correct route notification routing.",
                        "evidence_required": "Focused route notification evidence.",
                    },
                )
            ],
        )


class MalformedRouteWorker(WorkerAdapter):
    def run(
        self,
        instance: RoleInstanceConfig,
        message: Message,
        flow_state: FlowState,
    ) -> AgentRunResult:
        return AgentRunResult(
            status="completed",
            message="Emit malformed route for runtime recovery.",
            routes=[
                RouteRequest(
                    target_role="product-manager",
                    message_type="sdlc.implementation",
                    payload={"lifecycle_state": "implementation"},
                )
            ],
        )


class MalformedHandoffWorker(WorkerAdapter):
    def run(
        self,
        instance: RoleInstanceConfig,
        message: Message,
        flow_state: FlowState,
    ) -> AgentRunResult:
        return AgentRunResult(
            status="completed",
            message="Emit malformed legacy handoff for runtime recovery.",
            handoffs=[
                Handoff(
                    target_role="engineering",
                    message_type="sdlc.implementation",
                    payload={"lifecycle_state": "implementation"},
                )
            ],
        )


class ClarificationWorker(WorkerAdapter):
    def run(
        self,
        instance: RoleInstanceConfig,
        message: Message,
        flow_state: FlowState,
    ) -> AgentRunResult:
        return AgentRunResult(
            status="needs_clarification",
            message=(
                "Sponsor input needed: should the gateway support Teams DMs "
                "as well as all-agents channel messages?"
            ),
        )


class IndexedPublicationWorker(WorkerAdapter):
    def __init__(self, *, maintain_work_item_index: bool | None = None) -> None:
        self.maintain_work_item_index = maintain_work_item_index

    def run(
        self,
        instance: RoleInstanceConfig,
        message: Message,
        flow_state: FlowState,
    ) -> AgentRunResult:
        return AgentRunResult(
            status="completed",
            message="Completed with indexed publication.",
            document_updates=[
                DocumentUpdate(
                    path=flow_state.artifact_path,
                    content="# Business Brief\n\nReview status: `approved`\n",
                    purpose="Capture business analysis evidence.",
                    review_status="approved",
                    index_summary="Runtime maintained index coverage.",
                    maintain_work_item_index=self.maintain_work_item_index,
                )
            ],
            handoffs=[
                Handoff(
                    target_role="product-manager",
                    message_type="sdlc.product_definition",
                    payload={"summary": "Ready for product definition."},
                )
            ],
        )


class RecursiveIndexPublicationWorker(WorkerAdapter):
    def run(
        self,
        instance: RoleInstanceConfig,
        message: Message,
        flow_state: FlowState,
    ) -> AgentRunResult:
        return AgentRunResult(
            status="completed",
            message="Published an index document directly.",
            document_updates=[
                DocumentUpdate(
                    path="work-items/work-recursive-index/00-index.md",
                    content="# Work Item Index: Direct index update\n",
                    maintain_work_item_index=True,
                )
            ],
        )


class FailingIndexArtifactStore(ArtifactStore):
    def write_maintained_index(self, **kwargs):
        raise RuntimeError("simulated maintained index failure")


class FailingHumanGateRequestStore:
    def ensure_request(self, **kwargs):
        raise RuntimeError("simulated durable request write failure")


class FailingConnectorOutbox(FileConnectorOutbox):
    def enqueue(self, message):
        raise RuntimeError("simulated connector enqueue failure")


def _release_response_runtime(
    tmp_path: Path,
    *,
    work_item_id: str = "slice-release",
    response_request_id: str = "human-response-1",
):
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
    artifacts = ArtifactStore(
        tmp_path / "workspace",
        mesh_config.project.project_id,
        journal,
    )
    runtime = AgentRuntime(
        message_store,
        artifacts,
        journal,
        mesh_config.project,
        StubCodexWorkerAdapter(),
        connector_outbox=connector_outbox,
        response_types=mesh_config.response_types,
    )
    gate = mesh_config.project.flow.states["release_review"].gates[0]
    store = FileHumanGateRequestStore(
        tmp_path / "state",
        mesh_config.project.project_id,
    )
    request, _ = store.ensure_request(
        work_item_id=work_item_id,
        work_item_type="slice",
        lifecycle_state="release_review",
        gate=gate,
        response_request_id=response_request_id,
    )
    store.mark_enqueue_succeeded(
        request.response_request_id,
        connector_message_id="connector-message-1",
    )
    return mesh_config, runtime, message_store, connector_outbox, journal, store, request


def _enqueue_release_response(
    message_store: FileMessageStore,
    *,
    target_role: str = "release-manager",
    work_item_id: str = "slice-release",
    work_item_type: str = "slice",
    lifecycle_state: str = "release_review",
    gate_id: str = "release_decision_response",
    response_type: str | None = "approve_not_approve",
    response_request_id: str = "human-response-1",
    response_value: str = "approved",
    source: str = "test",
    connector_origin_authenticated: bool = False,
    prevalidated_human_response: bool = False,
) -> None:
    payload = {
        "title": "Human response received",
        "summary": "Release sponsor responded.",
        "work_item_id": work_item_id,
        "work_item_type": work_item_type,
        "lifecycle_state": lifecycle_state,
        "gate_id": gate_id,
        "response_type": response_type,
        "response_request_id": response_request_id,
        "response_value": response_value,
        "responder": "release-sponsor",
        "connector_origin_authenticated": connector_origin_authenticated,
        "prevalidated_human_response": prevalidated_human_response,
    }
    message_store.enqueue(
        Message.create(
            role_id=target_role,
            message_type="human_response.received",
            payload=payload,
            source=source,
        )
    )


def _work_completed_statuses(journal: EventJournal) -> list[str]:
    return [
        event["status"]
        for event in journal.read_all()
        if event["event_type"] == "work_completed"
    ]


def _project_with_review_loop_notifications(mesh_config):
    policy = mesh_config.project.notification_policy
    overrides = dict(policy.event_overrides)
    overrides["lifecycle.review_loop_requested"] = NotificationEventOverrideConfig(
        visibility="notify"
    )
    return replace(
        mesh_config.project,
        notification_policy=replace(policy, event_overrides=overrides),
    )


def test_project_configured_sdlc_flow_reaches_engineering(tmp_path: Path) -> None:
    mesh_config = load_mesh_config(Path.cwd())
    journal = EventJournal(tmp_path / "state", mesh_config.project.project_id)
    message_store = FileMessageStore(tmp_path / "state", mesh_config.project.project_id, journal)
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
                "title": "Local Runtime Skeleton",
                "summary": "Prove the local spine.",
                "work_item_id": "slice-local-runtime",
                "work_item_type": "slice",
                "lifecycle_state": "business_analysis",
                "auto_handoff": True,
            },
            source="test",
        )
    )

    for role_id in ["business-analyst", "product-manager"]:
        instance_id = f"agentic-mesh-dev.{role_id}.1"
        assert runtime.run_once(instance_id, mesh_config.instances[instance_id])

    store = FileHumanGateRequestStore(tmp_path / "state", mesh_config.project.project_id)
    product_requests = store.read_by_work_item("slice-local-runtime")
    assert len(product_requests) == 1
    assert product_requests[0].gate_id == "product_definition_sponsor_signoff"
    assert connector_outbox.pending_count("approvals") == 1

    _enqueue_release_response(
        message_store,
        target_role="product-manager",
        work_item_id="slice-local-runtime",
        lifecycle_state="product_definition",
        gate_id="product_definition_sponsor_signoff",
        response_request_id=product_requests[0].response_request_id,
    )
    assert runtime.run_once(
        "agentic-mesh-dev.product-manager.1",
        mesh_config.instances["agentic-mesh-dev.product-manager.1"],
    )
    assert runtime.run_once(
        "agentic-mesh-dev.product-manager.1",
        mesh_config.instances["agentic-mesh-dev.product-manager.1"],
    )

    for role_id in [
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

    work_item_root = tmp_path / "workspace" / "work-items" / "slice-local-runtime"
    business_briefs = work_item_root / "10-business-brief.md"
    stories = work_item_root / "20-product-definition.md"
    ux_notes = work_item_root / "30-experience-design.md"
    enterprise_notes = work_item_root / "40-enterprise-alignment.md"
    solution_notes = work_item_root / "50-solution-design.md"
    security_notes = work_item_root / "60-security-review.md"
    platform_notes = work_item_root / "70-platform-readiness.md"
    engineering_plan = work_item_root / "80-implementation-plan.md"
    assert "business_analysis" in business_briefs.read_text(encoding="utf-8")
    assert "Local Runtime Skeleton" in stories.read_text(encoding="utf-8")
    assert "experience_design" in ux_notes.read_text(encoding="utf-8")
    assert "enterprise_alignment" in enterprise_notes.read_text(encoding="utf-8")
    assert "solution_design" in solution_notes.read_text(encoding="utf-8")
    assert "security_review" in security_notes.read_text(encoding="utf-8")
    assert "platform_readiness" in platform_notes.read_text(encoding="utf-8")
    assert "implementation_planning" in engineering_plan.read_text(encoding="utf-8")
    assert "agentic-mesh-dev.engineering.1" in engineering_plan.read_text(encoding="utf-8")
    assert not (tmp_path / "workspace" / "docs" / "product" / "stories.md").exists()

    event_types = [event["event_type"] for event in journal.read_all()]
    assert "message_accepted" in event_types
    assert "work_claimed" in event_types
    assert "agent_run_started" in event_types
    assert "documentation_updated" in event_types
    assert "handoff_emitted" in event_types
    assert "message_routed" in event_types
    assert "work_completed" in event_types
    assert "local_trace_span" in event_types


def test_runtime_marks_misrouted_claim_failed_instead_of_leaving_claimed(
    tmp_path: Path,
) -> None:
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
            role_id="product-manager",
            message_type="sdlc.product_definition",
            payload={
                "title": "Misrouted",
                "summary": "This state belongs to business analysis.",
                "work_item_id": "work-misrouted",
                "work_item_type": "slice",
                "lifecycle_state": "business_analysis",
            },
            source="test",
        )
    )

    assert runtime.run_once(
        "agentic-mesh-dev.product-manager.1",
        mesh_config.instances["agentic-mesh-dev.product-manager.1"],
    )

    assert message_store.pending_count("product-manager") == 0
    summary = message_store.work_item_summary("work-misrouted", ["product-manager"])
    assert summary["product-manager"]["claimed"] == 0
    assert summary["product-manager"]["completed"] == 1
    events = journal.read_all()
    assert [event["event_type"] for event in events] == [
        "message_accepted",
        "work_claimed",
        "agent_run_failed",
        "work_completed",
    ]
    assert events[-1]["status"] == "failed"


def test_needs_clarification_result_requests_durable_sponsor_response(
    tmp_path: Path,
) -> None:
    mesh_config = load_mesh_config(Path.cwd())
    state_root = tmp_path / "state"
    journal = EventJournal(state_root, mesh_config.project.project_id)
    message_store = FileMessageStore(state_root, mesh_config.project.project_id, journal)
    connector_outbox = FileConnectorOutbox(
        state_root,
        mesh_config.project.project_id,
        journal,
    )
    artifacts = ArtifactStore(tmp_path / "workspace", mesh_config.project.project_id, journal)
    runtime = AgentRuntime(
        message_store,
        artifacts,
        journal,
        mesh_config.project,
        ClarificationWorker(),
        connector_outbox=connector_outbox,
        response_types=mesh_config.response_types,
    )
    message_store.enqueue(
        Message.create(
            role_id="product-manager",
            message_type="sdlc.product_definition",
            payload={
                "title": "Gateway bot",
                "summary": "Define the gateway bot product shape.",
                "work_item_id": "work-gateway",
                "work_item_type": "slice",
                "lifecycle_state": "product_definition",
                "source_channel": "all-agents",
                "teams_activity_id": "activity-root",
                "teams_reply_to_activity_id": "activity-root",
                "teams_conversation_id": "conversation-1",
                "teams_service_url": "https://smba.trafficmanager.net/uk/",
            },
            source="test",
        )
    )

    assert runtime.run_once(
        "agentic-mesh-dev.product-manager.1",
        mesh_config.instances["agentic-mesh-dev.product-manager.1"],
    )

    request = connector_outbox.claim_next("all-agents", "test-connector")
    assert request is not None
    assert request.type == "human_response.requested"
    assert request.payload["gate_id"] == "sponsor_clarification_response"
    assert request.payload["response_type"] == "multiline_text"
    assert request.payload["teams_reply_to_activity_id"] == "activity-root"
    gate_store = FileHumanGateRequestStore(state_root, mesh_config.project.project_id)
    stored = gate_store.read(request.payload["response_request_id"])
    assert stored is not None
    assert stored.status == "waiting_for_response"
    event_types = [event["event_type"] for event in journal.read_all()]
    assert "human_response_requested" in event_types


def test_runtime_normalises_worker_handoff_payload_from_flow(tmp_path: Path) -> None:
    mesh_config = load_mesh_config(Path.cwd())
    journal = EventJournal(tmp_path / "state", mesh_config.project.project_id)
    message_store = FileMessageStore(tmp_path / "state", mesh_config.project.project_id, journal)
    artifacts = ArtifactStore(tmp_path / "workspace", mesh_config.project.project_id, journal)
    runtime = AgentRuntime(
        message_store,
        artifacts,
        journal,
        mesh_config.project,
        IncompleteHandoffWorker(),
    )

    message_store.enqueue(
        Message.create(
            role_id="business-analyst",
            message_type="sdlc.business_analysis",
            payload={
                "title": "Work Queue V0",
                "summary": "Create the project work queue abstraction.",
                "work_item_id": "work-queue-v0",
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

    product_message = message_store.claim_next("product-manager", "test-product")
    assert product_message is not None
    assert product_message.payload["lifecycle_state"] == "product_definition"
    assert product_message.payload["previous_lifecycle_state"] == "business_analysis"
    assert product_message.payload["work_item_id"] == "work-queue-v0"
    assert product_message.payload["work_item_type"] == "slice"
    assert product_message.payload["title"] == "Work Queue V0"
    assert product_message.payload["source_message_id"]
    artifact = (
        tmp_path
        / "workspace"
        / "work-items"
        / "work-queue-v0"
        / "10-business-brief.md"
    )
    assert artifact.read_text(encoding="utf-8") == "Completed business framing.\n"

    handoff_events = [
        event
        for event in journal.read_all()
        if event["event_type"] == "handoff_emitted"
    ]
    assert handoff_events[0]["target_lifecycle_state"] == "product_definition"


def test_runtime_maintains_work_item_indexes_before_handoff(tmp_path: Path) -> None:
    mesh_config = load_mesh_config(Path.cwd())
    journal = EventJournal(tmp_path / "state", mesh_config.project.project_id)
    message_store = FileMessageStore(tmp_path / "state", mesh_config.project.project_id, journal)
    artifacts = ArtifactStore(tmp_path / "workspace", mesh_config.project.project_id, journal)
    runtime = AgentRuntime(
        message_store,
        artifacts,
        journal,
        mesh_config.project,
        IndexedPublicationWorker(),
    )

    message_store.enqueue(
        Message.create(
            role_id="business-analyst",
            message_type="sdlc.business_analysis",
            payload={
                "title": "Runtime index maintenance",
                "summary": "Prove controlled publication updates indexes.",
                "work_item_id": "work-runtime-index",
                "work_item_type": "slice",
                "lifecycle_state": "business_analysis",
                "queue_item_id": "queue-runtime-index",
                "source_anchor": {
                    "source_anchor_ref": "source:runtime-index",
                    "received_at": "2026-06-06T00:00:00+00:00",
                },
            },
            source="test",
        )
    )

    assert runtime.run_once(
        "agentic-mesh-dev.business-analyst.1",
        mesh_config.instances["agentic-mesh-dev.business-analyst.1"],
    )

    local_index = (
        tmp_path / "workspace" / "work-items" / "work-runtime-index" / "00-index.md"
    )
    global_index = tmp_path / "workspace" / "work-items" / "index.md"
    assert "Lifecycle state at last index update: `business_analysis`" in local_index.read_text(
        encoding="utf-8"
    )
    assert (
        "| [10-business-brief.md](10-business-brief.md) | "
        "Capture business analysis evidence. | `business-analyst` | `approved` |"
    ) in local_index.read_text(encoding="utf-8")
    assert "[work-runtime-index](work-runtime-index/00-index.md)" in global_index.read_text(
        encoding="utf-8"
    )
    assert message_store.pending_count("product-manager") == 1

    event_types = [event["event_type"] for event in journal.read_all()]
    assert event_types.index("work_item_index_updated") < event_types.index(
        "handoff_emitted"
    )
    assert event_types.index("work_items_index_updated") < event_types.index(
        "handoff_emitted"
    )


def test_runtime_respects_disabled_work_item_index_maintenance(tmp_path: Path) -> None:
    mesh_config = load_mesh_config(Path.cwd())
    journal = EventJournal(tmp_path / "state", mesh_config.project.project_id)
    message_store = FileMessageStore(tmp_path / "state", mesh_config.project.project_id, journal)
    artifacts = ArtifactStore(tmp_path / "workspace", mesh_config.project.project_id, journal)
    runtime = AgentRuntime(
        message_store,
        artifacts,
        journal,
        mesh_config.project,
        IndexedPublicationWorker(maintain_work_item_index=False),
    )

    message_store.enqueue(
        Message.create(
            role_id="business-analyst",
            message_type="sdlc.business_analysis",
            payload={
                "title": "No index maintenance",
                "summary": "Worker opted out of maintained index updates.",
                "work_item_id": "work-index-disabled",
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

    assert not (
        tmp_path / "workspace" / "work-items" / "work-index-disabled" / "00-index.md"
    ).exists()
    event_types = [event["event_type"] for event in journal.read_all()]
    assert "work_item_index_updated" not in event_types
    assert "work_items_index_updated" not in event_types
    assert "handoff_emitted" in event_types


def test_runtime_does_not_recursively_maintain_index_documents(tmp_path: Path) -> None:
    mesh_config = load_mesh_config(Path.cwd())
    journal = EventJournal(tmp_path / "state", mesh_config.project.project_id)
    message_store = FileMessageStore(tmp_path / "state", mesh_config.project.project_id, journal)
    artifacts = ArtifactStore(tmp_path / "workspace", mesh_config.project.project_id, journal)
    runtime = AgentRuntime(
        message_store,
        artifacts,
        journal,
        mesh_config.project,
        RecursiveIndexPublicationWorker(),
    )

    message_store.enqueue(
        Message.create(
            role_id="business-analyst",
            message_type="sdlc.business_analysis",
            payload={
                "title": "Recursive index guard",
                "summary": "Do not index an index document.",
                "work_item_id": "work-recursive-index",
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

    event_types = [event["event_type"] for event in journal.read_all()]
    assert "documentation_updated" in event_types
    assert "work_item_index_updated" not in event_types
    assert "work_items_index_updated" not in event_types


def test_runtime_blocks_handoff_when_index_maintenance_fails(tmp_path: Path) -> None:
    mesh_config = load_mesh_config(Path.cwd())
    journal = EventJournal(tmp_path / "state", mesh_config.project.project_id)
    message_store = FileMessageStore(tmp_path / "state", mesh_config.project.project_id, journal)
    artifacts = FailingIndexArtifactStore(
        tmp_path / "workspace",
        mesh_config.project.project_id,
        journal,
    )
    runtime = AgentRuntime(
        message_store,
        artifacts,
        journal,
        mesh_config.project,
        IndexedPublicationWorker(),
    )

    message_store.enqueue(
        Message.create(
            role_id="business-analyst",
            message_type="sdlc.business_analysis",
            payload={
                "title": "Index failure blocks handoff",
                "summary": "Preserve artifact but fail before routing.",
                "work_item_id": "work-index-failure",
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

    artifact = (
        tmp_path / "workspace" / "work-items" / "work-index-failure" / "10-business-brief.md"
    )
    assert artifact.exists()
    assert message_store.pending_count("product-manager") == 0
    summary = message_store.work_item_summary("work-index-failure", ["business-analyst"])
    assert summary["business-analyst"]["completion_statuses"] == [
        "needs_runtime_recovery",
    ]
    event_types = [event["event_type"] for event in journal.read_all()]
    assert "work_item_index_update_failed" in event_types
    assert "problem_status_recorded" in event_types
    assert "recovery_status_recorded" in event_types
    assert "handoff_emitted" not in event_types


def test_runtime_normalises_configured_handoff_matched_by_target_state(
    tmp_path: Path,
) -> None:
    mesh_config = load_mesh_config(Path.cwd())
    journal = EventJournal(tmp_path / "state", mesh_config.project.project_id)
    message_store = FileMessageStore(tmp_path / "state", mesh_config.project.project_id, journal)
    artifacts = ArtifactStore(tmp_path / "workspace", mesh_config.project.project_id, journal)
    runtime = AgentRuntime(
        message_store,
        artifacts,
        journal,
        mesh_config.project,
        StateMatchedHandoffWorker(),
    )

    message_store.enqueue(
        Message.create(
            role_id="business-analyst",
            message_type="sdlc.business_analysis",
            payload={
                "title": "Normalise lifecycle handoff",
                "summary": "Treat target-state matched handoff as configured flow.",
                "work_item_id": "work-state-matched-handoff",
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

    product_message = message_store.claim_next("product-manager", "test-product")
    assert product_message is not None
    assert product_message.type == "sdlc.product_definition"
    assert product_message.payload["lifecycle_state"] == "product_definition"
    assert product_message.payload.get("out_of_flow") is not True


def test_runtime_preserves_queue_provenance_in_handoff_payload(tmp_path: Path) -> None:
    mesh_config = load_mesh_config(Path.cwd())
    journal = EventJournal(tmp_path / "state", mesh_config.project.project_id)
    message_store = FileMessageStore(tmp_path / "state", mesh_config.project.project_id, journal)
    artifacts = ArtifactStore(tmp_path / "workspace", mesh_config.project.project_id, journal)
    runtime = AgentRuntime(
        message_store,
        artifacts,
        journal,
        mesh_config.project,
        IncompleteHandoffWorker(),
    )

    message_store.enqueue(
        Message.create(
            role_id="business-analyst",
            message_type="sdlc.business_analysis",
            payload={
                "title": "Work Queue V0",
                "summary": "Create the project work queue abstraction.",
                "work_item_id": "work-queue-v0",
                "work_item_type": "slice",
                "lifecycle_state": "business_analysis",
                "queue_item_id": "queue-123",
                "source_anchor": {
                    "connector_type": "teams",
                    "connector_id": "teams-bot-listener",
                    "source_scope": "all-agents",
                    "source_anchor_ref": "source:abc123",
                    "display_label": "Nich in all-agents",
                    "received_at": "2026-06-04T10:00:00+00:00",
                },
            },
            source="work-queue:queue-123",
        )
    )

    assert runtime.run_once(
        "agentic-mesh-dev.business-analyst.1",
        mesh_config.instances["agentic-mesh-dev.business-analyst.1"],
    )

    product_message = message_store.claim_next("product-manager", "test-product")
    assert product_message is not None
    assert product_message.payload["queue_item_id"] == "queue-123"
    assert product_message.payload["source_anchor"]["source_anchor_ref"] == "source:abc123"


def test_runtime_allows_reasoned_out_of_flow_handoff(tmp_path: Path) -> None:
    mesh_config = load_mesh_config(Path.cwd())
    journal = EventJournal(tmp_path / "state", mesh_config.project.project_id)
    message_store = FileMessageStore(tmp_path / "state", mesh_config.project.project_id, journal)
    artifacts = ArtifactStore(tmp_path / "workspace", mesh_config.project.project_id, journal)
    runtime = AgentRuntime(
        message_store,
        artifacts,
        journal,
        mesh_config.project,
        OutOfFlowHandoffWorker(),
    )

    message_store.enqueue(
        Message.create(
            role_id="business-analyst",
            message_type="sdlc.business_analysis",
            payload={
                "title": "Fix handoff routing",
                "summary": "Repair a contained runtime handoff bug.",
                "work_item_id": "work-handoff-repair",
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

    engineering_message = message_store.claim_next("engineering", "test-engineering")
    assert engineering_message is not None
    assert engineering_message.payload["lifecycle_state"] == "implementation"
    assert engineering_message.payload["out_of_flow"] is True
    assert "contained runtime defect" in engineering_message.payload["out_of_flow_reason"]

    routed_events = [
        event
        for event in journal.read_all()
        if event["event_type"] == "message_routed"
    ]
    assert routed_events[0]["target_lifecycle_state"] == "implementation"
    assert routed_events[0]["out_of_flow"] is True


def test_correction_route_notification_targets_source_anchor_when_enabled(
    tmp_path: Path,
) -> None:
    mesh_config = load_mesh_config(Path.cwd())
    project = _project_with_review_loop_notifications(mesh_config)
    journal = EventJournal(tmp_path / "state", project.project_id)
    message_store = FileMessageStore(tmp_path / "state", project.project_id, journal)
    connector_outbox = FileConnectorOutbox(tmp_path / "state", project.project_id, journal)
    artifacts = ArtifactStore(tmp_path / "workspace", project.project_id, journal)
    FileSourceRouteStore(tmp_path / "state", project.project_id).write(
        SourceRouteRecord(
            source_anchor_ref="source:qa-thread",
            connector_type="teams",
            connector_id="teams",
            route_label="QA review thread",
            route_kind="thread_reply",
            raw_route={"route": "qa-review-thread"},
            capabilities=("thread_reply",),
        )
    )
    runtime = AgentRuntime(
        message_store,
        artifacts,
        journal,
        project,
        CorrectionRouteWorker(),
        connector_outbox=connector_outbox,
    )

    message_store.enqueue(
        Message.create(
            role_id="qa-engineer",
            message_type="sdlc.quality_review",
            payload={
                "title": "QA correction",
                "summary": "Request implementation correction.",
                "work_item_id": "work-qchr",
                "work_item_type": "slice",
                "lifecycle_state": "quality_review",
                "queue_item_id": "queue-qchr",
                "source_anchor": {
                    "connector_type": "teams",
                    "connector_id": "teams",
                    "source_scope": "qa-review",
                    "source_anchor_ref": "source:qa-thread",
                    "display_label": "QA review thread",
                },
            },
            source="test",
        )
    )

    assert runtime.run_once(
        "agentic-mesh-dev.qa-engineer.1",
        mesh_config.instances["agentic-mesh-dev.qa-engineer.1"],
    )

    engineering_message = message_store.claim_next("engineering", "test-engineering")
    assert engineering_message is not None
    assert engineering_message.type == "sdlc.consult.implementation"
    assert engineering_message.payload["lifecycle_state"] == "implementation"
    assert engineering_message.payload["correction_status"] == "correction_requested"
    assert connector_outbox.pending_count("qa-review-thread") == 1
    assert connector_outbox.pending_count("all-agents") == 0
    connector_message = connector_outbox.claim_next("qa-review-thread", "test-connector")
    assert connector_message is not None
    assert connector_message.type == "route_status.updated"
    assert connector_message.payload["fallback"] is False
    assert connector_message.payload["route_status"]["route_kind"] == "configured_correction"
    assert (
        connector_message.payload["route_status"]["source_anchor_ref"]
        == "source:qa-thread"
    )


def test_correction_route_notification_falls_back_to_status_surface_when_source_missing(
    tmp_path: Path,
) -> None:
    mesh_config = load_mesh_config(Path.cwd())
    project = _project_with_review_loop_notifications(mesh_config)
    journal = EventJournal(tmp_path / "state", project.project_id)
    message_store = FileMessageStore(tmp_path / "state", project.project_id, journal)
    connector_outbox = FileConnectorOutbox(tmp_path / "state", project.project_id, journal)
    artifacts = ArtifactStore(tmp_path / "workspace", project.project_id, journal)
    runtime = AgentRuntime(
        message_store,
        artifacts,
        journal,
        project,
        CorrectionRouteWorker(),
        connector_outbox=connector_outbox,
    )

    message_store.enqueue(
        Message.create(
            role_id="qa-engineer",
            message_type="sdlc.quality_review",
            payload={
                "title": "QA correction",
                "summary": "Request implementation correction.",
                "work_item_id": "work-qchr-fallback",
                "work_item_type": "slice",
                "lifecycle_state": "quality_review",
                "source_anchor": {
                    "source_anchor_ref": "source:missing-thread",
                    "display_label": "Missing source thread",
                },
            },
            source="test",
        )
    )

    assert runtime.run_once(
        "agentic-mesh-dev.qa-engineer.1",
        mesh_config.instances["agentic-mesh-dev.qa-engineer.1"],
    )

    assert connector_outbox.pending_count("all-agents") == 1
    connector_message = connector_outbox.claim_next("all-agents", "test-connector")
    assert connector_message is not None
    assert connector_message.payload["fallback"] is True
    assert (
        connector_message.payload["route_status"]["source_anchor_ref"]
        == "source:missing-thread"
    )
    events = journal.read_all()
    queued = [
        event
        for event in events
        if event["event_type"] == "route_status_connector_message_queued"
    ]
    assert queued[0]["fallback_used"] is True
    assert queued[0]["fallback_reason"] == "source_route_missing"


def test_malformed_route_records_structured_problem_status_fields(
    tmp_path: Path,
) -> None:
    mesh_config = load_mesh_config(Path.cwd())
    journal = EventJournal(tmp_path / "state", mesh_config.project.project_id)
    message_store = FileMessageStore(tmp_path / "state", mesh_config.project.project_id, journal)
    artifacts = ArtifactStore(tmp_path / "workspace", mesh_config.project.project_id, journal)
    runtime = AgentRuntime(
        message_store,
        artifacts,
        journal,
        mesh_config.project,
        MalformedRouteWorker(),
    )

    message_store.enqueue(
        Message.create(
            role_id="qa-engineer",
            message_type="sdlc.quality_review",
            payload={
                "title": "Malformed route probe",
                "summary": "Probe runtime recovery.",
                "work_item_id": "work-malformed-route",
                "work_item_type": "slice",
                "lifecycle_state": "quality_review",
            },
            source="test",
        )
    )

    assert runtime.run_once(
        "agentic-mesh-dev.qa-engineer.1",
        mesh_config.instances["agentic-mesh-dev.qa-engineer.1"],
    )

    assert message_store.pending_count("product-manager") == 0
    current = ProblemStatusStore(
        tmp_path / "state",
        mesh_config.project.project_id,
    ).read_current("work-malformed-route")
    assert current is not None
    assert current["failure_class"] == "malformed_route"
    assert current["attempted_target_role"] == "product-manager"
    assert current["attempted_message_type"] == "sdlc.implementation"
    assert current["attempted_lifecycle_state"] == "implementation"
    assert current["expected_owner"] == "engineering"
    assert current["matched_configured_route"] is None
    assert current["route_validation_errors"]


def test_malformed_legacy_handoff_records_structured_problem_status(
    tmp_path: Path,
) -> None:
    mesh_config = load_mesh_config(Path.cwd())
    journal = EventJournal(tmp_path / "state", mesh_config.project.project_id)
    message_store = FileMessageStore(tmp_path / "state", mesh_config.project.project_id, journal)
    artifacts = ArtifactStore(tmp_path / "workspace", mesh_config.project.project_id, journal)
    runtime = AgentRuntime(
        message_store,
        artifacts,
        journal,
        mesh_config.project,
        MalformedHandoffWorker(),
    )

    message_store.enqueue(
        Message.create(
            role_id="qa-engineer",
            message_type="sdlc.quality_review",
            payload={
                "title": "Malformed handoff probe",
                "summary": "Probe legacy handoff recovery.",
                "work_item_id": "work-malformed-handoff",
                "work_item_type": "slice",
                "lifecycle_state": "quality_review",
            },
            source="test",
        )
    )

    assert runtime.run_once(
        "agentic-mesh-dev.qa-engineer.1",
        mesh_config.instances["agentic-mesh-dev.qa-engineer.1"],
    )

    assert message_store.pending_count("engineering") == 0
    current = ProblemStatusStore(
        tmp_path / "state",
        mesh_config.project.project_id,
    ).read_current("work-malformed-handoff")
    assert current is not None
    assert current["status"] == "needs_runtime_recovery"
    assert current["failure_class"] == "malformed_handoff"
    assert current["attempted_target_role"] == "engineering"
    assert current["attempted_lifecycle_state"] == "implementation"
    assert current["route_validation_errors"]


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
                    "auto_handoff": True,
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

    with patch.dict(
        "os.environ",
        {
            "AGENTIC_MESH_STATUS_BASE_URL": "https://status.example.com",
        },
    ):
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
    assert request.payload["approval_context"]["work_performed_summary"] == (
        "Confirm release readiness."
    )
    assert request.payload["approval_context"]["status_url"] == (
        "https://status.example.com/work-items/slice-release"
    )
    assert request.payload["approval_context"]["test_url"] == (
        "https://status.example.com/work-items/slice-release"
    )
    assert request.payload["approval_context"]["artifact_paths"] == [
        "work-items/slice-release/140-release-record.md"
    ]
    assert request.payload["approval_decision_view"]["schema_version"] == (
        "approval-decision-view-v1"
    )
    assert request.payload["approval_decision_view"]["context_completeness"] == "complete"
    store = FileHumanGateRequestStore(tmp_path / "state", mesh_config.project.project_id)
    human_gate_requests = store.read_by_work_item("slice-release")
    assert len(human_gate_requests) == 1
    human_gate_request = human_gate_requests[0]
    assert request.payload["approval_request_id"] == human_gate_request.approval_request_id
    assert request.payload["response_request_id"] == human_gate_request.response_request_id
    assert request.payload["notification_attempt_id"] == (
        human_gate_request.current_notification_attempt_id
    )
    assert human_gate_request.status == "waiting_for_response"
    assert human_gate_request.connector_message_id == request.message_id
    assert human_gate_request.notification_attempts[-1]["status"] == "queued"
    assert human_gate_request.notification_attempts[-1]["connector_message_id"] == (
        request.message_id
    )
    stored_context = store.read_decision_context(
        human_gate_request.response_request_id,
    )
    assert stored_context is not None
    assert stored_context["description_summary"] == "Confirm release readiness."

    events = journal.read_all()
    assert "human_response_requested" in [
        event["event_type"]
        for event in events
    ]
    context_events = [
        event
        for event in events
        if event["event_type"] == "approval_decision_context_created"
    ]
    assert context_events[0]["context_completeness"] == "complete"
    completed = [
        event
        for event in events
        if event["event_type"] == "work_completed"
    ][0]
    assert completed["status"] == "waiting_for_human_response"


def test_human_response_gate_reuses_active_durable_request(tmp_path: Path) -> None:
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
    payload = {
        "title": "Release local runtime",
        "summary": "Confirm release readiness.",
        "work_item_id": "slice-release-retry",
        "work_item_type": "slice",
        "lifecycle_state": "release_review",
    }
    for _ in range(2):
        message_store.enqueue(
            Message.create(
                role_id="release-manager",
                message_type="sdlc.release_review",
                payload=payload,
                source="test",
            )
        )
        assert runtime.run_once(
            "agentic-mesh-dev.release-manager.1",
            mesh_config.instances["agentic-mesh-dev.release-manager.1"],
        )

    assert connector_outbox.pending_count("approvals") == 1
    store = FileHumanGateRequestStore(tmp_path / "state", mesh_config.project.project_id)
    human_gate_requests = store.read_by_work_item("slice-release-retry")
    assert len(human_gate_requests) == 1
    assert human_gate_requests[0].status == "waiting_for_response"
    assert human_gate_requests[0].connector_message_id
    assert len(human_gate_requests[0].notification_attempts) == 1
    event_types = [event["event_type"] for event in journal.read_all()]
    assert event_types.count("human_response_requested") == 1
    assert event_types.count("human_response_request_reused") == 1


def test_human_response_gate_persistence_failure_enqueues_no_card(
    tmp_path: Path,
) -> None:
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
    runtime.human_gate_store = FailingHumanGateRequestStore()
    message_store.enqueue(
        Message.create(
            role_id="release-manager",
            message_type="sdlc.release_review",
            payload={
                "title": "Release local runtime",
                "summary": "Confirm release readiness.",
                "work_item_id": "slice-release-persistence-failed",
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

    assert connector_outbox.pending_count("approvals") == 0
    store = FileHumanGateRequestStore(tmp_path / "state", mesh_config.project.project_id)
    assert store.read_by_work_item("slice-release-persistence-failed") == []
    events = journal.read_all()
    failures = [
        event
        for event in events
        if event["event_type"] == "human_response_request_failed"
    ]
    assert failures[0]["reason"] == "approval_request_persistence_failed"
    completed = [
        event
        for event in events
        if event["event_type"] == "work_completed"
    ][0]
    assert completed["status"] == "human_response_request_failed"


def test_human_response_gate_enqueue_failure_marks_durable_request_failed(
    tmp_path: Path,
) -> None:
    mesh_config = load_mesh_config(Path.cwd())
    journal = EventJournal(tmp_path / "state", mesh_config.project.project_id)
    message_store = FileMessageStore(
        tmp_path / "state",
        mesh_config.project.project_id,
        journal,
    )
    connector_outbox = FailingConnectorOutbox(
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
                "work_item_id": "slice-release-enqueue-failed",
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

    assert connector_outbox.pending_count("approvals") == 0
    store = FileHumanGateRequestStore(tmp_path / "state", mesh_config.project.project_id)
    human_gate_requests = store.read_by_work_item("slice-release-enqueue-failed")
    assert len(human_gate_requests) == 1
    request = human_gate_requests[0]
    assert request.status == "request_failed"
    assert request.status_reason == "approval_request_enqueue_failed"
    assert request.notification_attempts[-1]["status"] == "failed"
    assert request.notification_attempts[-1]["safe_reason"] == (
        "approval_request_enqueue_failed"
    )
    completed = [
        event
        for event in journal.read_all()
        if event["event_type"] == "work_completed"
    ][0]
    assert completed["status"] == "human_response_request_failed"


def test_runtime_approval_context_rejects_auth_admin_and_loopback_status_urls() -> None:
    with patch.dict(
        "os.environ",
        {"AGENTIC_MESH_AUTH_ADMIN_URL": "http://127.0.0.1:8100/auth/status"},
        clear=True,
    ):
        assert AgentRuntime._work_item_status_url("slice-release") is None

    with patch.dict(
        "os.environ",
        {"AGENTIC_MESH_STATUS_BASE_URL": "http://controller.local"},
        clear=True,
    ):
        assert AgentRuntime._work_item_status_url("slice-release") is None

    with patch.dict(
        "os.environ",
        {"AGENTIC_MESH_STATUS_BASE_URL": "http://127.0.0.1:8100"},
        clear=True,
    ):
        assert AgentRuntime._work_item_status_url("slice-release") is None

    with patch.dict(
        "os.environ",
        {"AGENTIC_MESH_STATUS_BASE_URL": "https://status.example.com"},
        clear=True,
    ):
        assert AgentRuntime._work_item_status_url("slice-release") == (
            "https://status.example.com/work-items/slice-release"
        )


def test_human_response_gate_omits_status_links_without_approved_https_base(
    tmp_path: Path,
) -> None:
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

    with patch.dict(
        "os.environ",
        {"AGENTIC_MESH_AUTH_ADMIN_URL": "http://127.0.0.1:8100/auth/status"},
        clear=True,
    ):
        assert runtime.run_once(
            "agentic-mesh-dev.release-manager.1",
            mesh_config.instances["agentic-mesh-dev.release-manager.1"],
        )

    request = connector_outbox.claim_next("approvals", "local-connector")
    assert request is not None
    approval_context = request.payload["approval_context"]
    assert "status_url" not in approval_context
    assert "test_url" not in approval_context


def test_sdlc_handoff_is_dashboard_only_by_default(tmp_path: Path) -> None:
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
                "auto_handoff": True,
            },
            source="test",
        )
    )

    assert runtime.run_once(
        "agentic-mesh-dev.business-analyst.1",
        mesh_config.instances["agentic-mesh-dev.business-analyst.1"],
    )

    assert connector_outbox.pending_count("product") == 0
    event_types = [event["event_type"] for event in journal.read_all()]
    assert "notification_policy_evaluated" in event_types
    assert "handoff_connector_message_queued" not in event_types


def test_direct_sponsor_directive_runs_without_lifecycle_handoff_or_gate(
    tmp_path: Path,
) -> None:
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
            message_type=MESSAGE_TYPE_SPONSOR_DIRECTIVE_REQUESTED,
            payload={
                "title": "Adopt this project",
                "summary": "Analyse the repo from your release perspective.",
                "work_item_id": "work-adoption",
                "work_item_type": "directive",
                "work_mode": "direct_broadcast",
                "requested_roles": ["release-manager"],
                "output_path": "documents/analysis/release-manager.md",
                "source_channel": "all-agents",
                "git_branch": "codex/work-adoption-adopt-this-project",
                "publication": {
                    "mode": "git_branch",
                    "branch": "codex/work-adoption-adopt-this-project",
                    "status": "open",
                    "commit_policy": "commit_and_push_after_all_roles_terminal",
                },
            },
            source="test",
        )
    )

    assert runtime.run_once(
        "agentic-mesh-dev.release-manager.1",
        mesh_config.instances["agentic-mesh-dev.release-manager.1"],
    )

    assert connector_outbox.pending_count("approvals") == 0
    assert connector_outbox.pending_count("release") == 2
    assert connector_outbox.pending_count("all-agents") == 1
    started = connector_outbox.claim_next("release", "test-connector")
    assert started is not None
    assert started.type == "sponsor_directive.started"
    assert started.payload["role_id"] == "release-manager"
    assert started.payload["status"] == "started"
    completed = connector_outbox.claim_next("release", "test-connector")
    assert completed is not None
    assert completed.type == "sponsor_directive.completed"
    assert completed.payload["role_id"] == "release-manager"
    assert completed.payload["status"] == "completed"
    assert completed.payload["artifact_paths"] == [
        "documents/analysis/release-manager.md"
    ]
    assert completed.payload["git_branch"] == "codex/work-adoption-adopt-this-project"
    publish_ready = connector_outbox.claim_next("all-agents", "test-connector")
    assert publish_ready is not None
    assert publish_ready.type == "sponsor_directive.publish_ready"
    assert publish_ready.payload["publication"]["status"] == "ready_to_commit_and_push"
    assert publish_ready.payload["artifact_paths"] == [
        "documents/analysis/release-manager.md"
    ]
    assert message_store.pending_count("delivery-manager") == 0
    artifact = (
        tmp_path
        / "workspace"
        / "documents"
        / "analysis"
        / "release-manager.md"
    )
    content = artifact.read_text(encoding="utf-8")
    assert "not currently inside the lifecycle flow" in content
    assert "Use the available handoff routes as options, not commands" in content
    event_types = [event["event_type"] for event in journal.read_all()]
    assert "agent_directive_run_started" in event_types
    assert "directive_status_connector_message_queued" in event_types
    assert "directive_publish_ready_connector_message_queued" in event_types
    assert "handoff_emitted" not in event_types
    assert "human_response_requested" not in event_types


class ConversationWorkerAdapter(WorkerAdapter):
    def run(
        self,
        instance: RoleInstanceConfig,
        message: Message,
        flow_state: FlowState,
    ) -> AgentRunResult:
        return AgentRunResult(
            status="completed",
            message="In-role answer sent. I would propose tracked work if needed.",
            terminal_tool="status.reply",
        )


class InvalidConversationWorkerAdapter(WorkerAdapter):
    def run(
        self,
        instance: RoleInstanceConfig,
        message: Message,
        flow_state: FlowState,
    ) -> AgentRunResult:
        return AgentRunResult(
            status="completed",
            message="This should not be treated as a successful conversation.",
            document_updates=[
                DocumentUpdate(
                    path="documents/analysis/product-manager.md",
                    content="This should not be published from conversation.",
                )
            ],
            handoffs=[
                Handoff(
                    target_role="delivery-manager",
                    message_type="sdlc.intake",
                    payload={"summary": "This should not be routed informally."},
                )
            ],
            terminal_tool="status.reply",
        )


class QueueProposalConversationWorkerAdapter(WorkerAdapter):
    def run(
        self,
        instance: RoleInstanceConfig,
        message: Message,
        flow_state: FlowState,
    ) -> AgentRunResult:
        return AgentRunResult(
            status="completed",
            message="I've proposed this as a tracked slice.",
            queue_proposals=[
                QueueProposal(
                    title="Build gateway DM bot",
                    summary="Create the dedicated Agentic Mesh gateway bot.",
                    owner_role="delivery-manager",
                    recommended_work_item_type="slice",
                )
            ],
            terminal_tool="status.reply",
        )


class ReleaseWorkItemActionConversationWorkerAdapter(WorkerAdapter):
    def run(
        self,
        instance: RoleInstanceConfig,
        message: Message,
        flow_state: FlowState,
    ) -> AgentRunResult:
        return AgentRunResult(
            status="completed",
            message="Release reconciliation actions recorded.",
            work_item_actions=[
                WorkItemAction(
                    action="close",
                    work_item_id="work-close",
                    reason="Sponsor approved closure as sufficiently resolved.",
                    disposition="sponsor_exception",
                    source_tool="work_item.close",
                ),
                WorkItemAction(
                    action="override_blocker",
                    work_item_id="work-blocked",
                    reason="Sponsor explicitly overrode the stale blocker.",
                    source_tool="work_item.override_blocker",
                ),
                WorkItemAction(
                    action="reopen_flow",
                    work_item_id="work-reopen",
                    reason="QA must perform a fresh check after runtime recovery.",
                    target_role="qa-engineer",
                    lifecycle_state="quality_review",
                    work_item_type="slice",
                    source_tool="work_item.reopen_flow",
                ),
            ],
            terminal_tool="status.reply",
        )


class ProductManagerHandoffConversationWorkerAdapter(WorkerAdapter):
    def run(
        self,
        instance: RoleInstanceConfig,
        message: Message,
        flow_state: FlowState,
    ) -> AgentRunResult:
        return AgentRunResult(
            status="completed",
            message="I handed the existing slice to UX.",
            work_item_actions=[
                WorkItemAction(
                    action="handoff",
                    work_item_id="work-product-handoff",
                    reason="Product definition is accepted and ready for UX.",
                    target_role="ux-designer",
                    source_lifecycle_state="product_definition",
                    lifecycle_state="experience_design",
                    work_item_type="slice",
                    summary="Continue dashboard presentation refinement.",
                    source_tool="work_item.handoff",
                )
            ],
            terminal_tool="status.reply",
        )


class ProductManagerOutOfFlowHandoffConversationWorkerAdapter(WorkerAdapter):
    def run(
        self,
        instance: RoleInstanceConfig,
        message: Message,
        flow_state: FlowState,
    ) -> AgentRunResult:
        return AgentRunResult(
            status="completed",
            message="I handed the existing slice to architecture for a focused check.",
            work_item_actions=[
                WorkItemAction(
                    action="handoff",
                    work_item_id="work-product-out-of-flow",
                    reason=(
                        "The sponsor raised architecture scope concerns that should "
                        "be resolved before UX continues."
                    ),
                    target_role="enterprise-architect",
                    source_lifecycle_state="product_definition",
                    lifecycle_state="enterprise_alignment",
                    work_item_type="slice",
                    summary="Check architecture impact before UX continuation.",
                    source_tool="work_item.handoff",
                )
            ],
            terminal_tool="status.reply",
        )


def test_direct_conversation_does_not_publish_artifacts_or_handoffs(
    tmp_path: Path,
) -> None:
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
        ConversationWorkerAdapter(),
        connector_outbox=connector_outbox,
        response_types=mesh_config.response_types,
    )
    message_store.enqueue(
        Message.create(
            role_id="product-manager",
            message_type=MESSAGE_TYPE_DIRECT_CONVERSATION_REQUESTED,
            payload={
                "title": "Explain product tradeoffs",
                "summary": "Can you explain the product scope trade-offs in role?",
                "text": "Can you explain the product scope trade-offs in role?",
                "conversation_mode": "targeted",
                "requested_roles": ["product-manager"],
                "target_role": "product-manager",
                "source_channel": "dm",
                "teams_conversation_id": "personal-conversation-1",
                "teams_reply_to_activity_id": "activity-product-manager-dm",
                "teams_service_url": "https://smba.trafficmanager.net/uk/",
            },
            source="teams:teams-bot-listener:dm",
        )
    )

    assert runtime.run_once(
        "agentic-mesh-dev.product-manager.1",
        mesh_config.instances["agentic-mesh-dev.product-manager.1"],
    )

    assert connector_outbox.pending_count("product") == 0
    assert connector_outbox.pending_count("dm") == 1
    completed = connector_outbox.claim_next("dm", "test-connector")
    assert completed is not None
    assert completed.type == "conversation.completed"
    assert completed.payload["status"] == "completed"
    assert completed.payload["status_message"] == (
        "In-role answer sent. I would propose tracked work if needed."
    )
    assert "work_item_id" not in completed.payload
    assert completed.payload["source_channel"] == "dm"
    assert completed.payload["teams_conversation_id"] == "personal-conversation-1"
    assert completed.payload["teams_reply_to_activity_id"] == (
        "activity-product-manager-dm"
    )
    assert not (tmp_path / "workspace" / "documents").exists()
    assert message_store.pending_count("delivery-manager") == 0
    assert FileWorkQueueStore(
        tmp_path / "state",
        mesh_config.project.project_id,
        journal,
    ).list_items() == []

    event_types = [event["event_type"] for event in journal.read_all()]
    assert "agent_conversation_run_started" in event_types
    assert "conversation_status_connector_message_queued" in event_types
    assert "conversation_document_updates_ignored" not in event_types
    assert "conversation_handoffs_ignored" not in event_types
    assert "directive_publish_ready_connector_message_queued" not in event_types
    assert "handoff_emitted" not in event_types


def test_direct_conversation_records_problem_for_disallowed_outputs(
    tmp_path: Path,
) -> None:
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
        InvalidConversationWorkerAdapter(),
        connector_outbox=connector_outbox,
        response_types=mesh_config.response_types,
    )
    message = message_store.enqueue(
        Message.create(
            role_id="product-manager",
            message_type=MESSAGE_TYPE_DIRECT_CONVERSATION_REQUESTED,
            payload={
                "title": "Explain product tradeoffs",
                "summary": "Can you explain the product scope trade-offs in role?",
                "text": "Can you explain the product scope trade-offs in role?",
                "conversation_mode": "targeted",
                "requested_roles": ["product-manager"],
                "target_role": "product-manager",
                "source_channel": "dm",
                "teams_conversation_id": "personal-conversation-1",
                "teams_reply_to_activity_id": "activity-product-manager-dm",
                "teams_service_url": "https://smba.trafficmanager.net/uk/",
            },
            source="teams:teams-bot-listener:dm",
        )
    )

    assert runtime.run_once(
        "agentic-mesh-dev.product-manager.1",
        mesh_config.instances["agentic-mesh-dev.product-manager.1"],
    )

    assert not (tmp_path / "workspace" / "documents").exists()
    problem = ProblemStatusStore(
        tmp_path / "state",
        mesh_config.project.project_id,
    ).read_current(message.message_id)
    assert problem is not None
    assert "not allowed for conversational work" in problem["reason"]
    event_types = [event["event_type"] for event in journal.read_all()]
    assert "problem_status_recorded" in event_types
    assert "conversation_status_connector_message_queued" not in event_types
    assert "conversation_document_updates_ignored" not in event_types
    assert "conversation_handoffs_ignored" not in event_types


def test_direct_conversation_queue_proposal_creates_work_queue_item(
    tmp_path: Path,
) -> None:
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
        QueueProposalConversationWorkerAdapter(),
        connector_outbox=connector_outbox,
        response_types=mesh_config.response_types,
    )
    message_store.enqueue(
        Message.create(
            role_id="product-manager",
            message_type=MESSAGE_TYPE_DIRECT_CONVERSATION_REQUESTED,
            payload={
                "title": "Build the gateway bot",
                "summary": "Please get the team to build the gateway bot.",
                "text": "Please get the team to build the gateway bot.",
                "conversation_mode": "targeted",
                "requested_roles": ["product-manager"],
                "target_role": "product-manager",
                "source_channel": "all-agents",
                "source_anchor": {
                    "connector_type": "teams",
                    "connector_id": "teams-bot-listener",
                    "source_scope": "all-agents",
                    "source_anchor_ref": "source:gateway",
                    "display_label": "Gateway request",
                    "received_at": "2026-06-10T10:00:00+00:00",
                },
            },
            source="teams:teams-bot-listener:all-agents",
        )
    )

    assert runtime.run_once(
        "agentic-mesh-dev.product-manager.1",
        mesh_config.instances["agentic-mesh-dev.product-manager.1"],
    )

    queue_items = FileWorkQueueStore(
        tmp_path / "state",
        mesh_config.project.project_id,
        journal,
    ).list_items()
    assert len(queue_items) == 1
    assert queue_items[0].title == "Build gateway DM bot"
    assert queue_items[0].owner_role == "delivery-manager"
    assert queue_items[0].status == "promoted"
    assert queue_items[0].promotion is not None
    assert queue_items[0].promotion.target_role == "product-manager"
    assert queue_items[0].promotion.lifecycle_state == "product_definition"
    assert queue_items[0].metadata["created_by_safe_output"] is True
    lifecycle_message = message_store.claim_next("product-manager", "test-worker")
    assert lifecycle_message is not None
    assert lifecycle_message.payload["queue_item_id"] == queue_items[0].queue_item_id
    assert lifecycle_message.payload["queue_promotion_kind"] == "intake"
    completed = connector_outbox.claim_next("all-agents", "test-connector")
    assert completed is not None
    assert completed.type == "conversation.completed"
    assert completed.payload["status_message"] == "I've proposed this as a tracked slice."
    event_types = [event["event_type"] for event in journal.read_all()]
    assert "safe_output_queue_proposal_captured" in event_types
    assert "safe_output_queue_proposal_promoted" in event_types


def test_release_manager_direct_conversation_can_apply_work_item_actions(
    tmp_path: Path,
) -> None:
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
    problem_store = ProblemStatusStore(
        tmp_path / "state",
        mesh_config.project.project_id,
    )
    problem_store.write_current(
        role_problem_status(
            status="blocked",
            message="Historical blocker to override.",
            project_id=mesh_config.project.project_id,
            role_id="qa-engineer",
            role_instance_id="agentic-mesh-dev.qa-engineer.1",
            work_item_id="work-blocked",
            work_item_type="slice",
            lifecycle_state="quality_review",
            queue_item_id=None,
            source_message_id="msg-old-blocker",
            source_anchor=None,
            correlation_id="corr-old-blocker",
            status_url=None,
        )
    )
    runtime = AgentRuntime(
        message_store,
        artifacts,
        journal,
        mesh_config.project,
        ReleaseWorkItemActionConversationWorkerAdapter(),
        connector_outbox=connector_outbox,
        response_types=mesh_config.response_types,
    )
    message_store.enqueue(
        Message.create(
            role_id="release-manager",
            message_type=MESSAGE_TYPE_DIRECT_CONVERSATION_REQUESTED,
            payload={
                "title": "Release reconciliation",
                "summary": "Close and reopen specific work items.",
                "text": (
                    "Close work-close, override work-blocked, and reopen "
                    "work-reopen for QA."
                ),
                "conversation_mode": "targeted",
                "requested_roles": ["release-manager"],
                "target_role": "release-manager",
                "source_channel": "all-agents",
            },
            source="teams:teams-bot-listener:all-agents",
        )
    )

    assert runtime.run_once(
        "agentic-mesh-dev.release-manager.1",
        mesh_config.instances["agentic-mesh-dev.release-manager.1"],
    )

    assert problem_store.read_current("work-blocked") is None
    assert message_store.pending_count("qa-engineer") == 1
    reopened = message_store.claim_next("qa-engineer", "test-qa")
    assert reopened is not None
    assert reopened.type == "sdlc.quality_review"
    assert reopened.payload["work_item_id"] == "work-reopen"
    assert reopened.payload["lifecycle_state"] == "quality_review"
    completed = connector_outbox.claim_next("all-agents", "test-connector")
    assert completed is not None
    assert completed.type == "conversation.completed"
    assert (
        completed.payload["status_message"]
        == "Release reconciliation actions recorded."
    )
    event_types = [event["event_type"] for event in journal.read_all()]
    assert "safe_output_work_item_closed" in event_types
    assert "safe_output_work_item_blocker_overridden" in event_types
    assert "safe_output_work_item_flow_reopened" in event_types


def test_direct_conversation_can_handoff_existing_work_item_when_role_owns_state(
    tmp_path: Path,
) -> None:
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
        ProductManagerHandoffConversationWorkerAdapter(),
        connector_outbox=connector_outbox,
        response_types=mesh_config.response_types,
    )
    message_store.enqueue(
        Message.create(
            role_id="product-manager",
            message_type=MESSAGE_TYPE_DIRECT_CONVERSATION_REQUESTED,
            payload={
                "title": "Continue existing slice",
                "summary": "The product definition is ready. Hand it to UX.",
                "text": "Continue work-product-handoff to UX.",
                "conversation_mode": "targeted",
                "requested_roles": ["product-manager"],
                "target_role": "product-manager",
                "source_channel": "dm",
                "work_item_id": "work-product-handoff",
                "work_item_type": "slice",
            },
            source="teams:teams-bot-listener:dm",
        )
    )
    item = runtime.work_queue.capture(
        title="Continue existing slice",
        summary="The product definition is ready. Hand it to UX.",
        owner_role="product-manager",
        recommended_work_item_type="slice",
        source_anchor=SourceAnchor(
            connector_type="teams",
            connector_id="teams-bot-listener",
            source_scope="dm",
            source_message_id="activity-product-handoff",
            actor="sponsor",
            received_at="2026-06-10T10:00:00+00:00",
            display_label="Product handoff test",
        ),
    )
    runtime.work_queue.promote(
        item.queue_item_id,
        actor_role="product-manager",
        message_store=message_store,
        target_role="product-manager",
        lifecycle_state="product_definition",
        work_item_id="work-product-handoff",
        work_item_type="slice",
        message_type="sdlc.product_definition",
    )

    assert runtime.run_once(
        "agentic-mesh-dev.product-manager.1",
        mesh_config.instances["agentic-mesh-dev.product-manager.1"],
    )

    assert message_store.pending_count("ux-designer") == 1
    handoff = message_store.claim_next("ux-designer", "test-ux")
    assert handoff is not None
    assert handoff.type == "sdlc.experience_design"
    assert handoff.payload["work_item_id"] == "work-product-handoff"
    assert handoff.payload["previous_lifecycle_state"] == "product_definition"
    assert handoff.payload["lifecycle_state"] == "experience_design"
    assert handoff.payload["route_kind"] == "configured_handoff"
    completed = connector_outbox.claim_next("dm", "test-connector")
    assert completed is not None
    assert completed.type == "conversation.completed"
    assert completed.payload["status_message"] == "I handed the existing slice to UX."
    event_types = [event["event_type"] for event in journal.read_all()]
    assert "safe_output_work_item_handed_off" in event_types
    assert "handoff_emitted" in event_types


def test_direct_conversation_can_handoff_existing_work_item_out_of_flow_with_reason(
    tmp_path: Path,
) -> None:
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
        ProductManagerOutOfFlowHandoffConversationWorkerAdapter(),
        connector_outbox=connector_outbox,
        response_types=mesh_config.response_types,
    )
    message_store.enqueue(
        Message.create(
            role_id="product-manager",
            message_type=MESSAGE_TYPE_DIRECT_CONVERSATION_REQUESTED,
            payload={
                "title": "Architecture check needed",
                "summary": "Sponsor raised architecture scope concerns.",
                "text": "Send this to architecture before UX.",
                "conversation_mode": "targeted",
                "requested_roles": ["product-manager"],
                "target_role": "product-manager",
                "source_channel": "dm",
                "work_item_id": "work-product-out-of-flow",
                "work_item_type": "slice",
            },
            source="teams:teams-bot-listener:dm",
        )
    )
    item = runtime.work_queue.capture(
        title="Architecture check needed",
        summary="Sponsor raised architecture scope concerns.",
        owner_role="product-manager",
        recommended_work_item_type="slice",
        source_anchor=SourceAnchor(
            connector_type="teams",
            connector_id="teams-bot-listener",
            source_scope="dm",
            source_message_id="activity-product-out-of-flow",
            actor="sponsor",
            received_at="2026-06-10T10:00:00+00:00",
            display_label="Out-of-flow handoff test",
        ),
    )
    runtime.work_queue.promote(
        item.queue_item_id,
        actor_role="product-manager",
        message_store=message_store,
        target_role="product-manager",
        lifecycle_state="product_definition",
        work_item_id="work-product-out-of-flow",
        work_item_type="slice",
        message_type="sdlc.product_definition",
    )

    assert runtime.run_once(
        "agentic-mesh-dev.product-manager.1",
        mesh_config.instances["agentic-mesh-dev.product-manager.1"],
    )

    assert message_store.pending_count("enterprise-architect") == 1
    handoff = message_store.claim_next("enterprise-architect", "test-ea")
    assert handoff is not None
    assert handoff.type == "sdlc.enterprise_alignment"
    assert handoff.payload["work_item_id"] == "work-product-out-of-flow"
    assert handoff.payload["previous_lifecycle_state"] == "product_definition"
    assert handoff.payload["lifecycle_state"] == "enterprise_alignment"
    assert handoff.payload["route_kind"] == "reasoned_out_of_flow"
    assert handoff.payload["out_of_flow"] is True
    assert "architecture scope concerns" in handoff.payload["out_of_flow_reason"]
    event_types = [event["event_type"] for event in journal.read_all()]
    assert "safe_output_work_item_handed_off" in event_types


def test_queue_originated_publish_ready_targets_source_anchor(
    tmp_path: Path,
) -> None:
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
            message_type=MESSAGE_TYPE_SPONSOR_DIRECTIVE_REQUESTED,
            payload={
                "title": "Queue-originated publication",
                "summary": "Publish-ready should return to the queue source.",
                "work_item_id": "work-queue-publish",
                "work_item_type": "directive",
                "work_mode": "direct_targeted",
                "requested_roles": ["release-manager"],
                "output_path": "documents/analysis/release-manager.md",
                "queue_item_id": "queue-publish",
                "source_anchor": {
                    "connector_type": "teams",
                    "connector_id": "teams-bot-listener",
                    "source_scope": "sponsor-requests",
                    "source_anchor_ref": "source:publish",
                    "display_label": "Nich in sponsor-requests",
                    "received_at": "2026-06-04T10:00:00+00:00",
                },
                "source_channel": "all-agents",
                "git_branch": "codex/work-queue-publish",
                "publication": {
                    "mode": "git_branch",
                    "branch": "codex/work-queue-publish",
                    "status": "open",
                    "commit_policy": "commit_and_push_after_all_roles_terminal",
                },
            },
            source="work-queue:queue-publish",
        )
    )

    assert runtime.run_once(
        "agentic-mesh-dev.release-manager.1",
        mesh_config.instances["agentic-mesh-dev.release-manager.1"],
    )

    assert connector_outbox.pending_count("all-agents") == 0
    assert connector_outbox.pending_count("sponsor-requests") == 1
    publish_ready = connector_outbox.claim_next("sponsor-requests", "test-connector")
    assert publish_ready is not None
    assert publish_ready.type == "sponsor_directive.publish_ready"
    assert publish_ready.payload["queue_item_id"] == "queue-publish"
    assert publish_ready.payload["source_anchor"]["source_anchor_ref"] == "source:publish"
    assert publish_ready.payload["publication"]["status"] == "ready_to_commit_and_push"

    publish_events = [
        event
        for event in journal.read_all()
        if event["event_type"] == "directive_publish_ready_connector_message_queued"
    ]
    assert publish_events[0]["channel"] == "sponsor-requests"


class BlockedDirectiveWorkerAdapter(WorkerAdapter):
    def run(
        self,
        instance: RoleInstanceConfig,
        message: Message,
        flow_state: FlowState,
    ) -> AgentRunResult:
        return AgentRunResult(
            status="blocked",
            message="Cannot complete without an implementation handoff.",
            document_updates=[
                DocumentUpdate(
                    path=flow_state.artifact_path,
                    content="\n## Blocked\n\nThis should stay out of documents.\n",
                )
            ],
            handoffs=[],
        )


def test_blocked_directive_reports_chat_status_without_document_artifact(
    tmp_path: Path,
) -> None:
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
        BlockedDirectiveWorkerAdapter(),
        connector_outbox=connector_outbox,
        response_types=mesh_config.response_types,
    )

    message_store.enqueue(
        Message.create(
            role_id="delivery-manager",
            message_type=MESSAGE_TYPE_SPONSOR_DIRECTIVE_REQUESTED,
            payload={
                "title": "Create Mermaid CLI slice",
                "summary": "Create a small lifecycle Mermaid CLI slice.",
                "work_item_id": "work-mermaid",
                "work_item_type": "directive",
                "work_mode": "direct_targeted",
                "requested_roles": ["delivery-manager"],
                "output_path": "documents/analysis/delivery-manager.md",
                "source_channel": "all-agents",
                "git_branch": "codex/work-mermaid",
                "publication": {
                    "mode": "git_branch",
                    "branch": "codex/work-mermaid",
                    "status": "open",
                    "commit_policy": "commit_and_push_after_all_roles_terminal",
                },
            },
            source="test",
        )
    )

    assert runtime.run_once(
        "agentic-mesh-dev.delivery-manager.1",
        mesh_config.instances["agentic-mesh-dev.delivery-manager.1"],
    )

    assert connector_outbox.pending_count("delivery") == 2
    started = connector_outbox.claim_next("delivery", "test-connector")
    blocked = connector_outbox.claim_next("delivery", "test-connector")
    assert started is not None
    assert blocked is not None
    assert blocked.type == "sponsor_directive.completed"
    assert blocked.payload["status"] == "blocked"
    assert blocked.payload["status_message"] == (
        "Cannot complete without an implementation handoff."
    )
    assert blocked.payload["artifact_paths"] == []

    assert connector_outbox.pending_count("all-agents") == 2
    source_notices = [
        connector_outbox.claim_next("all-agents", "test-connector"),
        connector_outbox.claim_next("all-agents", "test-connector"),
    ]
    notice_types = {notice.type for notice in source_notices if notice is not None}
    assert notice_types == {
        "problem_status.updated",
        "sponsor_directive.publish_ready",
    }
    publish_summary = next(
        notice
        for notice in source_notices
        if notice is not None and notice.type == "sponsor_directive.publish_ready"
    )
    assert publish_summary.type == "sponsor_directive.publish_ready"
    assert publish_summary.payload["publication"]["status"] == "terminal_with_blockers"
    assert publish_summary.payload["terminal_status"] == "terminal_with_blockers"
    assert publish_summary.payload["blocked_roles"] == ["delivery-manager"]
    assert publish_summary.payload["artifact_paths"] == []

    artifact = tmp_path / "workspace" / "documents" / "analysis" / "delivery-manager.md"
    assert not artifact.exists()


def test_human_response_received_continues_release_review(tmp_path: Path) -> None:
    (
        mesh_config,
        runtime,
        message_store,
        connector_outbox,
        journal,
        store,
        request,
    ) = _release_response_runtime(tmp_path)
    _enqueue_release_response(message_store)

    assert runtime.run_once(
        "agentic-mesh-dev.release-manager.1",
        mesh_config.instances["agentic-mesh-dev.release-manager.1"],
    )

    assert connector_outbox.pending_count("approvals") == 0
    assert store.read(request.response_request_id).status == "completed"  # type: ignore[union-attr]
    event_types = [event["event_type"] for event in journal.read_all()]
    assert "human_response_recorded" in event_types
    assert "human_response_continuation_enqueued" in event_types
    completed = [
        event
        for event in journal.read_all()
        if event["event_type"] == "work_completed"
    ][0]
    assert completed["status"] == "human_response_recorded"
    continuation = message_store.claim_next(
        "release-manager",
        "agentic-mesh-dev.release-manager.1",
    )
    assert continuation is not None
    assert continuation.type == "sdlc.release_review"
    assert continuation.payload["continuation_reason"] == "human_response_recorded"
    assert continuation.payload["human_response"]["response_value"] == "approved"
    message_store.complete(continuation, "continuation_claim_inspected")


def test_human_response_continuation_does_not_request_same_gate_again(
    tmp_path: Path,
) -> None:
    (
        mesh_config,
        runtime,
        message_store,
        connector_outbox,
        _journal,
        _store,
        _request,
    ) = _release_response_runtime(tmp_path)
    _enqueue_release_response(message_store)

    assert runtime.run_once(
        "agentic-mesh-dev.release-manager.1",
        mesh_config.instances["agentic-mesh-dev.release-manager.1"],
    )
    assert runtime.run_once(
        "agentic-mesh-dev.release-manager.1",
        mesh_config.instances["agentic-mesh-dev.release-manager.1"],
    )

    assert connector_outbox.pending_count("approvals") == 0
    assert _work_completed_statuses(_journal) == [
        "human_response_recorded",
        "completed",
    ]


def test_human_response_received_records_not_approved_without_satisfying_gate(
    tmp_path: Path,
) -> None:
    (
        mesh_config,
        runtime,
        message_store,
        _connector_outbox,
        journal,
        store,
        request,
    ) = _release_response_runtime(tmp_path)
    _enqueue_release_response(message_store, response_value="not_approved")

    assert runtime.run_once(
        "agentic-mesh-dev.release-manager.1",
        mesh_config.instances["agentic-mesh-dev.release-manager.1"],
    )

    stored = store.read(request.response_request_id)
    assert stored is not None
    assert stored.status == "not_approved"
    assert _work_completed_statuses(journal) == ["human_response_not_satisfied"]
    event_types = [event["event_type"] for event in journal.read_all()]
    assert "human_response_not_approved" in event_types


@pytest.mark.parametrize(
    ("case", "message_overrides", "expected_reason"),
    [
        (
            "missing request",
            {"response_request_id": "missing-human-response"},
            "stale_or_unknown_request",
        ),
        (
            "mismatched work item",
            {"work_item_id": "slice-other"},
            "work_item_mismatch",
        ),
        (
            "mismatched lifecycle",
            {"lifecycle_state": "delivery_readiness"},
            "lifecycle_state_mismatch",
        ),
        (
            "mismatched gate",
            {"gate_id": "release_owner_review"},
            "gate_id_mismatch",
        ),
        (
            "invalid value",
            {"response_value": "maybe"},
            "unrecognized_response_value",
        ),
        (
            "unauthenticated authoritative connector submit",
            {"source": "teams:teams-bot-listener"},
            "connector_origin_unauthenticated",
        ),
    ],
)
def test_human_response_received_invalid_paths_are_audit_only(
    tmp_path: Path,
    case: str,
    message_overrides: dict[str, str],
    expected_reason: str,
) -> None:
    (
        mesh_config,
        runtime,
        message_store,
        _connector_outbox,
        journal,
        store,
        request,
    ) = _release_response_runtime(tmp_path)
    params = {
        "work_item_id": "slice-release",
        "work_item_type": "slice",
        "lifecycle_state": "release_review",
        "gate_id": "release_decision_response",
        "response_type": "approve_not_approve",
        "response_request_id": "human-response-1",
        "response_value": "approved",
        "source": "test",
        "connector_origin_authenticated": False,
    }
    params.update(message_overrides)
    _enqueue_release_response(message_store, **params)

    assert runtime.run_once(
        "agentic-mesh-dev.release-manager.1",
        mesh_config.instances["agentic-mesh-dev.release-manager.1"],
    ), case

    assert _work_completed_statuses(journal) == ["human_response_not_satisfied"]
    invalid_events = [
        event
        for event in journal.read_all()
        if event["event_type"] == "human_response_invalid"
    ]
    assert invalid_events
    assert invalid_events[0]["validation_result"] == expected_reason
    stored = store.read(request.response_request_id)
    if expected_reason == "stale_or_unknown_request":
        assert stored is not None
        assert stored.status == "waiting_for_response"
    else:
        assert stored is not None
        assert stored.status == "invalid_response"
        assert stored.status_reason == expected_reason


def test_human_response_received_duplicate_terminal_is_audit_only(
    tmp_path: Path,
) -> None:
    (
        mesh_config,
        runtime,
        message_store,
        _connector_outbox,
        journal,
        store,
        request,
    ) = _release_response_runtime(tmp_path)
    _enqueue_release_response(message_store)
    assert runtime.run_once(
        "agentic-mesh-dev.release-manager.1",
        mesh_config.instances["agentic-mesh-dev.release-manager.1"],
    )
    continuation = message_store.claim_next(
        "release-manager",
        "agentic-mesh-dev.release-manager.1",
    )
    assert continuation is not None
    message_store.complete(continuation, "continuation_test_skipped")
    _enqueue_release_response(message_store)

    assert runtime.run_once(
        "agentic-mesh-dev.release-manager.1",
        mesh_config.instances["agentic-mesh-dev.release-manager.1"],
    )

    stored = store.read(request.response_request_id)
    assert stored is not None
    assert stored.status == "completed"
    assert _work_completed_statuses(journal) == [
        "human_response_recorded",
        "continuation_test_skipped",
        "human_response_not_satisfied",
    ]
    duplicate_events = [
        event
        for event in journal.read_all()
        if event["event_type"] == "human_response_duplicate"
    ]
    assert duplicate_events
    assert duplicate_events[0]["validation_result"] == "duplicate_same_value"


def test_prevalidated_human_response_duplicate_continues_release_review(
    tmp_path: Path,
) -> None:
    (
        mesh_config,
        runtime,
        message_store,
        _connector_outbox,
        journal,
        store,
        request,
    ) = _release_response_runtime(tmp_path)
    accepted, reason, stored = store.validate_response(
        project_id=mesh_config.project.project_id,
        work_item_id="slice-release",
        work_item_type="slice",
        lifecycle_state="release_review",
        gate_id="release_decision_response",
        response_type="approve_not_approve",
        response_request_id=request.response_request_id,
        responder="release-sponsor",
        response_value="approved",
        authenticated=True,
        authoritative=True,
    )
    assert accepted is True
    assert reason == "accepted"
    assert stored is not None
    assert stored.status == "completed"
    _enqueue_release_response(
        message_store,
        connector_origin_authenticated=True,
        prevalidated_human_response=True,
    )

    assert runtime.run_once(
        "agentic-mesh-dev.release-manager.1",
        mesh_config.instances["agentic-mesh-dev.release-manager.1"],
    )

    assert _work_completed_statuses(journal) in [
        ["human_response_recorded"],
        ["completed_after_human_response"],
    ]
    continuation = message_store.claim_next(
        "release-manager",
        "agentic-mesh-dev.release-manager.1",
    )
    if continuation is not None:
        assert continuation.type == "sdlc.release_review"
        assert continuation.payload["continuation_reason"] == "human_response_recorded"
    duplicate_events = [
        event
        for event in journal.read_all()
        if event["event_type"] == "human_response_duplicate"
    ]
    assert duplicate_events
    assert duplicate_events[0]["validation_result"] == "duplicate_same_value"


def test_human_response_received_late_terminal_response_is_audit_only(
    tmp_path: Path,
) -> None:
    (
        mesh_config,
        runtime,
        message_store,
        _connector_outbox,
        journal,
        store,
        request,
    ) = _release_response_runtime(tmp_path)
    store.mark_failed(
        request.response_request_id,
        reason="approval_request_recovery_required",
    )
    _enqueue_release_response(message_store)

    assert runtime.run_once(
        "agentic-mesh-dev.release-manager.1",
        mesh_config.instances["agentic-mesh-dev.release-manager.1"],
    )

    stored = store.read(request.response_request_id)
    assert stored is not None
    assert stored.status == "request_failed"
    assert _work_completed_statuses(journal) == ["human_response_not_satisfied"]
    invalid_events = [
        event
        for event in journal.read_all()
        if event["event_type"] == "human_response_invalid"
    ]
    assert invalid_events[0]["validation_result"] == "terminal_request"
