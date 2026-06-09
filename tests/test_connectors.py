import json
from pathlib import Path
from urllib.error import HTTPError

from agentic_mesh import telemetry
from agentic_mesh.config import load_mesh_config
from agentic_mesh.connectors import BotFrameworkTeamsConnectorAdapter
from agentic_mesh.connectors import LocalTeamsConnectorAdapter
from agentic_mesh.connectors import GraphTeamsChannelIngressAdapter
from agentic_mesh.connectors import GraphTeamsConnectorAdapter
from agentic_mesh.connectors import TeamsBotIngress
from agentic_mesh.connectors import build_human_response_card
from agentic_mesh.connectors import load_graph_token
from agentic_mesh.connectors import render_human_response_request_html
from agentic_mesh.connectors import render_route_status_html
from agentic_mesh.connectors import _work_item_status_url
from agentic_mesh.approval_decisions import build_requested_approval_decision
from agentic_mesh.human_gates import FileHumanGateRequestStore
from agentic_mesh.models import ConnectorMessage
from agentic_mesh.models import FlowGate
from agentic_mesh.models import FlowState
from agentic_mesh.journal import EventJournal
from agentic_mesh.messaging import build_human_response_request
from agentic_mesh.models import Message
from agentic_mesh.storage import FileConnectorOutbox
from agentic_mesh.storage import FileMessageStore
from agentic_mesh.work_queue import FileWorkQueueStore


class _HttpErrorBody:
    def read(self) -> bytes:
        return b'{"tenant_id":"raw-tenant","access_token":"raw-token","serviceUrl":"https://raw"}'

    def close(self) -> None:
        return None


class _StaticSecrets:
    def get(self, secret_ref: str) -> str:
        return f"value-for-{secret_ref}"


def _seed_release_approval_context(
    state_root: Path,
    *,
    project_id: str = "agentic-mesh-dev",
    response_request_id: str = "human-response-1",
    work_item_id: str = "slice-release",
    correlation_id: str = "corr-release",
) -> None:
    store = FileHumanGateRequestStore(state_root, project_id)
    request, _ = store.ensure_request(
        work_item_id=work_item_id,
        work_item_type="slice",
        lifecycle_state="release_review",
        gate=FlowGate(
            gate_id="release_decision_response",
            type="human_response",
            response_type="approve_not_approve",
            prompt="Record the final release decision for this work item.",
            requested_from="release-sponsor",
            channel="approvals",
            completion_criteria={"accepted_values": ["approved"]},
        ),
        response_request_id=response_request_id,
    )
    payload = {
        "response_request_id": request.response_request_id,
        "gate_id": request.gate_id,
        "response_type": request.response_type,
        "prompt": request.prompt,
        "project_id": project_id,
        "role_id": "release-manager",
        "work_item_id": work_item_id,
        "work_item_type": "slice",
        "lifecycle_state": "release_review",
        "title": "Release local runtime",
        "summary": "Old submit summary must not be authority.",
        "correlation_id": correlation_id,
        "approval_context": {
            "work_performed_summary": "Durable request-time release context.",
            "artifact_paths": [f"work-items/{work_item_id}/140-release-record.md"],
            "status_url": f"http://controller.local/work-items/{work_item_id}",
        },
        "response_template": {
            "input_mode": "choice",
            "options": [
                {"label": "Approve", "value": "approved"},
                {"label": "Not Approve", "value": "not_approved"},
            ],
        },
    }
    store.record_decision_context(
        response_request_id,
        decision_context=build_requested_approval_decision(payload).to_dict(),
    )


def _raise_provider_http_error(*args, **kwargs):
    raise HTTPError(
        url="https://graph.microsoft.com/raw",
        code=500,
        msg="failed",
        hdrs={},
        fp=_HttpErrorBody(),
    )


def test_graph_send_failure_records_redacted_error_class(
    tmp_path: Path,
    monkeypatch,
) -> None:
    mesh_config = load_mesh_config(Path.cwd())
    state_root = tmp_path / "state"
    journal = EventJournal(state_root, mesh_config.project.project_id)
    outbox = FileConnectorOutbox(state_root, mesh_config.project.project_id, journal)
    outbox.enqueue(
        ConnectorMessage.create(
            channel="all-agents",
            message_type="notification.event",
            payload={
                "event_kind": "work.completed",
                "title": "Done",
                "work_item_id": "work-redacted",
            },
            source="test",
        )
    )
    monkeypatch.setattr("agentic_mesh.connectors.request.urlopen", _raise_provider_http_error)
    connector = GraphTeamsConnectorAdapter(
        connector_id="graph-teams",
        project_id=mesh_config.project.project_id,
        connector_config=mesh_config.project.connectors["teams"],
        outbox=outbox,
        journal=journal,
        token="fake-token",
    )

    assert connector.process_once("all-agents") is True

    failure = [
        event
        for event in journal.read_all()
        if event["event_type"] == "teams_graph_message_failed"
    ][0]
    rendered = json.dumps(failure)
    assert failure["error"] == "GraphSendFailed500"
    assert failure["redacted_error_class"] == "GraphSendFailed500"
    assert "raw-token" not in rendered
    assert "tenant_id" not in rendered
    assert "serviceUrl" not in rendered
    assert "graph.microsoft.com/raw" not in rendered


def test_bot_send_failure_records_redacted_error_class(
    tmp_path: Path,
    monkeypatch,
) -> None:
    mesh_config = load_mesh_config(Path.cwd())
    state_root = tmp_path / "state"
    journal = EventJournal(state_root, mesh_config.project.project_id)
    outbox = FileConnectorOutbox(state_root, mesh_config.project.project_id, journal)
    outbox.enqueue(
        ConnectorMessage.create(
            channel="all-agents",
            message_type="notification.event",
            payload={
                "event_kind": "work.completed",
                "title": "Done",
                "work_item_id": "work-redacted",
                "role_id": "engineering",
            },
            source="test",
        )
    )
    monkeypatch.setattr("agentic_mesh.connectors.request.urlopen", _raise_provider_http_error)
    monkeypatch.setattr(
        BotFrameworkTeamsConnectorAdapter,
        "_bot_token",
        lambda self, app_id, app_secret: "fake-token",
    )
    connector = BotFrameworkTeamsConnectorAdapter(
        connector_id="bot-teams",
        project_id=mesh_config.project.project_id,
        connector_config=mesh_config.project.connectors["teams"],
        outbox=outbox,
        journal=journal,
        secrets=_StaticSecrets(),
    )

    assert connector.process_once("all-agents") is True

    failure = [
        event
        for event in journal.read_all()
        if event["event_type"] == "teams_bot_message_failed"
    ][0]
    rendered = json.dumps(failure)
    assert failure["error"] == "BotSendFailed500"
    assert failure["redacted_error_class"] == "BotSendFailed500"
    assert "raw-token" not in rendered
    assert "tenant_id" not in rendered
    assert "serviceUrl" not in rendered
    assert "graph.microsoft.com/raw" not in rendered


def test_local_teams_connector_renders_human_response_card(tmp_path: Path) -> None:
    mesh_config = load_mesh_config(Path.cwd())
    state_root = tmp_path / "state"
    journal = EventJournal(state_root, mesh_config.project.project_id)
    outbox = FileConnectorOutbox(state_root, mesh_config.project.project_id, journal)
    flow_state = mesh_config.project.flow.states["release_review"]
    gate = flow_state.gates[0]
    source_message = Message.create(
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
    request = build_human_response_request(
        gate=gate,
        response_type=mesh_config.response_types[gate.response_type],
        source_message=source_message,
        source_instance=mesh_config.instances["agentic-mesh-dev.release-manager.1"],
        flow_state=flow_state,
    )
    outbox.enqueue(request)

    connector = LocalTeamsConnectorAdapter(
        connector_id="local-teams-connector",
        project_id=mesh_config.project.project_id,
        state_root=state_root,
        outbox=outbox,
        journal=journal,
    )

    assert connector.process_once("approvals") is True
    assert connector.process_once("approvals") is False

    sent_files = list(
        (
            state_root
            / "projects"
            / mesh_config.project.project_id
            / "connectors"
            / "teams"
            / "approvals"
            / "sent"
        ).glob("*.json")
    )
    assert len(sent_files) == 1
    rendered = json.loads(sent_files[0].read_text(encoding="utf-8"))
    assert rendered["connector"] == "microsoft-teams-local"
    assert rendered["adaptive_card"]["type"] == "AdaptiveCard"
    assert [
        action["title"]
        for action in rendered["adaptive_card"]["actions"]
    ] == ["Approve", "Not Approve"]
    assert rendered["adaptive_card"]["actions"][0]["data"]["response_value"] == "approved"
    assert rendered["adaptive_card"]["actions"][0]["data"]["role_id"] == "release-manager"
    assert rendered["adaptive_card"]["actions"][0]["data"]["work_item_id"] == "slice-release"

    assert [event["event_type"] for event in journal.read_all()] == [
        "connector_message_queued",
        "connector_message_claimed",
        "teams_connector_message_prepared",
        "connector_message_completed",
    ]


def test_route_status_html_escapes_correction_fields_and_exposes_no_retry() -> None:
    message = ConnectorMessage.create(
        channel="engineering",
        message_type="route_status.updated",
        payload={
            "title": "<script>Correction</script>",
            "summary": "Fix route rendering.",
            "work_item_id": "work-correction",
            "route_status": {
                "work_item_id": "work-correction",
                "route_id": "route-123",
                "route_kind": "configured_correction",
                "route_status": "correction_requested",
                "source_role": "qa-engineer",
                "target_role": "engineering",
                "source_lifecycle_state": "quality_review",
                "target_lifecycle_state": "implementation",
                "defect_id": "DEF-QA-LIFE-001",
                "gate_id": "qa_owner_review",
                "required_change": "<b>Do not render raw HTML</b>",
                "evidence_required": "Implementation log and tests.",
                "status_url": "http://controller.local/work-items/work-correction",
            },
        },
        source="agentic-mesh-dev.qa-engineer.1",
    )

    rendered = render_route_status_html(message)

    assert "&lt;script&gt;Correction&lt;/script&gt;" in rendered
    assert "&lt;b&gt;Do not render raw HTML&lt;/b&gt;" in rendered
    assert "correction_requested" in rendered
    assert "DEF-QA-LIFE-001" in rendered
    assert "Retry same state" not in rendered


def test_human_response_rendering_includes_approval_context() -> None:
    message = ConnectorMessage.create(
        channel="approvals",
        message_type="human_response.requested",
        payload={
            "response_request_id": "human-response-1",
            "gate_id": "release_decision_response",
            "response_type": "approve_not_approve",
            "prompt": "Approve release?",
            "project_id": "agentic-mesh-dev",
            "role_id": "release-manager",
            "role_instance_id": "agentic-mesh-dev.release-manager.1",
            "work_item_id": "work-queue-v0",
            "work_item_type": "slice",
            "lifecycle_state": "release_review",
            "source_message_id": "msg-release",
            "correlation_id": "corr-release",
            "requested_at": "2026-06-04T10:00:00+00:00",
            "title": "Work Queue V0",
            "summary": "Old short summary.",
            "approval_context": {
                "work_performed_summary": "Implemented queue capture and QA evidence.",
                "completed_roles": ["engineering", "qa-engineer"],
                "blocked_roles": [],
                "artifact_paths": [
                    "work-items/work-queue-v0/100-implementation-log.md",
                    "work-items/work-queue-v0/110-quality-evidence.md",
                ],
                "status_url": "http://controller.local/work-items/work-queue-v0",
                "test_url": "http://controller.local/work-items/work-queue-v0",
                "test_url_label": "Review and test this work item",
            },
            "response_template": {
                "input_mode": "choice",
                "options": [
                    {"label": "Approve", "value": "approved"},
                    {"label": "Not Approve", "value": "not_approved"},
                ],
            },
        },
        source="agentic-mesh-dev.release-manager.1",
    )

    html = render_human_response_request_html(message)
    card = build_human_response_card(message)

    assert "Implemented queue capture and QA evidence." in html
    assert "work-items/work-queue-v0/110-quality-evidence.md" in html
    assert "Review and test this work item" in html
    assert card["actions"][0]["type"] == "Action.OpenUrl"
    assert card["actions"][0]["url"] == "http://controller.local/work-items/work-queue-v0"
    assert card["actions"][1]["type"] == "Action.Submit"
    submit_data = card["actions"][1]["data"]
    assert "title" not in submit_data
    assert "summary" not in submit_data
    assert "approval_context" not in submit_data
    assert "trace_context" not in submit_data
    assert "requested_at" not in submit_data
    rendered_card = json.dumps(card)
    assert "Implemented queue capture and QA evidence." in rendered_card
    assert "work-items/work-queue-v0/110-quality-evidence.md" in rendered_card


def test_sponsor_clarification_card_uses_readable_question_layout() -> None:
    message = ConnectorMessage.create(
        channel="all-agents",
        message_type="human_response.requested",
        payload={
            "response_request_id": "human-response-clarify",
            "gate_id": "sponsor_clarification_response",
            "response_type": "multiline_text",
            "prompt": (
                "Please answer these before product definition continues:\n"
                "1. What should the gateway bot be called?\n"
                "2. Should it support Teams DMs as well as channel posts?"
            ),
            "project_id": "agentic-mesh-dev",
            "role_id": "product-manager",
            "role_instance_id": "agentic-mesh-dev.product-manager.1",
            "work_item_id": "work-gateway",
            "work_item_type": "slice",
            "lifecycle_state": "product_definition",
            "title": "Agentic Mesh conversational gateway bot",
            "summary": "Define the gateway bot product shape.",
            "timeout": "PT48H",
            "on_timeout": "escalate",
            "response_template": {"input_mode": "multiline_text"},
            "approval_context": {
                "description_summary": "Define the gateway bot product shape.",
                "status_url": "http://controller.local/work-items/work-gateway",
            },
        },
        source="agentic-mesh-dev.product-manager.1",
    )

    html = render_human_response_request_html(message)
    card = build_human_response_card(message)
    rendered_card = json.dumps(card)

    assert "Sponsor input needed" in html
    assert "Questions / requested input" in html
    assert "What should the gateway bot be called?" in html
    assert "Work performed:" not in rendered_card
    assert "Sponsor input needed" in rendered_card
    assert "Questions / requested input" in rendered_card
    assert "What should the gateway bot be called?" in rendered_card
    assert card["body"][-1]["type"] == "Input.Text"
    assert card["body"][-1]["isMultiline"] is True
    assert card["actions"][-1]["title"] == "Submit answer"


def test_teams_ingress_records_human_response_submit(tmp_path: Path) -> None:
    mesh_config = load_mesh_config(Path.cwd())
    state_root = tmp_path / "state"
    journal = EventJournal(state_root, mesh_config.project.project_id)
    message_store = FileMessageStore(state_root, mesh_config.project.project_id, journal)
    store = FileHumanGateRequestStore(state_root, mesh_config.project.project_id)
    request, _ = store.ensure_request(
        work_item_id="slice-release",
        work_item_type="slice",
        lifecycle_state="release_review",
        gate=FlowGate(
            gate_id="release_decision_response",
            type="human_response",
            response_type="approve_not_approve",
            prompt="Record the final release decision for this work item.",
            requested_from="release-sponsor",
            channel="approvals",
            completion_criteria={"accepted_values": ["approved"]},
        ),
        response_request_id="human-response-1",
    )
    request_payload = {
        "response_request_id": request.response_request_id,
        "gate_id": request.gate_id,
        "response_type": request.response_type,
        "prompt": request.prompt,
        "project_id": mesh_config.project.project_id,
        "role_id": "release-manager",
        "work_item_id": "slice-release",
        "work_item_type": "slice",
        "lifecycle_state": "release_review",
        "title": "Release local runtime",
        "summary": "Old submit summary must not be authority.",
        "correlation_id": "corr-release",
        "approval_context": {
            "work_performed_summary": "Durable request-time release context.",
            "artifact_paths": ["work-items/slice-release/140-release-record.md"],
            "status_url": "http://controller.local/work-items/slice-release",
        },
        "response_template": {
            "input_mode": "choice",
            "options": [
                {"label": "Approve", "value": "approved"},
                {"label": "Not Approve", "value": "not_approved"},
            ],
        },
    }
    store.record_decision_context(
        request.response_request_id,
        decision_context=build_requested_approval_decision(request_payload).to_dict(),
    )
    ingress = TeamsBotIngress(
        connector_id="teams-bot-listener",
        project_id=mesh_config.project.project_id,
        state_root=state_root,
        message_store=message_store,
        journal=journal,
    )

    result = ingress.receive_activity(
        {
            "type": "invoke",
            "id": "activity/123",
            "serviceUrl": "https://smba.trafficmanager.net/emea/",
            "from": {"id": "user-1", "name": "Nich"},
            "conversation": {"id": "conversation-1"},
            "value": {
                "action": "human_response.submit",
                "role_id": "release-manager",
                "work_item_id": "slice-release",
                "work_item_type": "slice",
                "lifecycle_state": "release_review",
                "gate_id": "release_decision_response",
                "response_request_id": "human-response-1",
                "response_value": "approved",
                "correlation_id": "corr-release",
            },
        }
    )

    assert result["statusCode"] == 200
    assert result["type"] == "application/vnd.microsoft.card.adaptive"
    completed_card = result["value"]
    assert completed_card["type"] == "AdaptiveCard"
    assert [action["title"] for action in completed_card["actions"]] == ["Approve"]
    assert completed_card["actions"][0]["isEnabled"] is False
    assert completed_card["actions"][0]["data"]["action"] == "human_response.completed"
    rendered_completed = json.dumps(completed_card)
    assert "Release decision recorded" in rendered_completed
    assert "Durable request-time release context." in rendered_completed
    assert "Old submit summary must not be authority." not in rendered_completed
    assert message_store.pending_count("release-manager") == 1
    message = message_store.claim_next(
        "release-manager",
        "agentic-mesh-dev.release-manager.1",
    )
    assert message is not None
    assert message.type == "human_response.received"
    assert message.payload["response_value"] == "approved"
    assert message.payload["responder"] == "Nich"
    assert store.read(request.response_request_id).status == "completed"  # type: ignore[union-attr]

    event_types = [event["event_type"] for event in journal.read_all()]
    assert "teams_bot_activity_received" in event_types
    assert "human_response_received_from_teams" in event_types
    assert "human_response_completion_card_returned" in event_types


def test_teams_ingress_returns_non_final_card_without_durable_request(
    tmp_path: Path,
) -> None:
    mesh_config = load_mesh_config(Path.cwd())
    state_root = tmp_path / "state"
    journal = EventJournal(state_root, mesh_config.project.project_id)
    message_store = FileMessageStore(state_root, mesh_config.project.project_id, journal)
    _seed_release_approval_context(state_root, project_id=mesh_config.project.project_id)
    ingress = TeamsBotIngress(
        connector_id="teams-bot-listener",
        project_id=mesh_config.project.project_id,
        state_root=state_root,
        message_store=message_store,
        journal=journal,
    )

    result = ingress.receive_activity(
        {
            "type": "invoke",
            "id": "activity/missing",
            "from": {"id": "user-1", "name": "Nich"},
            "value": {
                "action": "human_response.submit",
                "role_id": "release-manager",
                "work_item_id": "slice-release",
                "work_item_type": "slice",
                "lifecycle_state": "release_review",
                "gate_id": "release_decision_response",
                "response_type": "approve_not_approve",
                "response_request_id": "missing-human-response",
                "response_value": "approved",
                "correlation_id": "corr-release",
            },
        }
    )

    rendered_card = json.dumps(result["value"])
    assert "Decision received for validation" in rendered_card
    assert "Release decision recorded" not in rendered_card
    assert message_store.pending_count("release-manager") == 0


def test_teams_ingress_routes_all_agents_message_to_direct_role_work(
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
    teams_config = mesh_config.project.connectors["teams"]
    ingress = TeamsBotIngress(
        connector_id="teams-bot-listener",
        project_id=mesh_config.project.project_id,
        state_root=state_root,
        message_store=message_store,
        journal=journal,
        connector_config=teams_config,
        project_config=mesh_config.project,
        connector_outbox=connector_outbox,
    )

    result = ingress.receive_activity(
        {
            "type": "message",
            "id": "activity/adoption",
            "serviceUrl": "https://smba.trafficmanager.net/uk/",
            "text": "<at>all-agents</at> Start an adoption process for Agentic Mesh.",
            "from": {"id": "user-1", "name": "Nich"},
            "conversation": {"id": "conversation-1"},
            "channelData": {
                "team": {"id": teams_config.team_id},
                "channel": {
                    "id": teams_config.channels["all-agents"].channel_id,
                    "name": "all-agents",
                },
            },
        }
    )

    assert result["routed"] is True
    roles = sorted(mesh_config.project.roles)
    assert result["target_roles"] == roles
    assert result["lifecycle_state"] is None
    assert result["work_item_id"].startswith("work-")
    assert all(message_store.pending_count(role_id) == 1 for role_id in roles)
    assert connector_outbox.pending_count("all-agents") == 1
    acknowledgement = connector_outbox.claim_next("all-agents", "test-connector")
    assert acknowledgement is not None
    assert acknowledgement.type == "sponsor_directive.acknowledged"
    assert acknowledgement.payload["role_count"] == len(roles)
    assert acknowledgement.payload["role_id"] == "delivery-manager"
    assert acknowledgement.payload["git_branch"].startswith("codex/")
    assert (
        acknowledgement.payload["publication"]["commit_policy"]
        == "commit_and_push_after_all_roles_terminal"
    )

    message = message_store.claim_next(
        "business-analyst",
        "agentic-mesh-dev.business-analyst.1",
    )
    assert message is not None
    assert message.type == "sponsor_directive.requested"
    assert message.source == "teams:teams-bot-listener:all-agents"
    assert message.payload["work_item_type"] == "directive"
    assert message.payload["work_mode"] == "direct_broadcast"
    assert message.payload["source_channel"] == "all-agents"
    assert message.payload["target_role"] == "business-analyst"
    assert message.payload["git_branch"] == acknowledgement.payload["git_branch"]
    assert message.payload["publication"]["mode"] == "git_branch"
    assert message.payload["output_path"] == "documents/analysis/business-analyst.md"
    assert message.payload["teams_from_name"] == "Nich"
    assert "Start an adoption process" in message.payload["summary"]
    assert "<at>" not in message.payload["summary"]

    event_types = [event["event_type"] for event in journal.read_all()]
    assert "teams_bot_activity_received" in event_types
    assert "message_accepted" in event_types
    assert "teams_all_agents_directive_routed" in event_types


def test_teams_ingress_captures_work_queue_before_direct_role_work(
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
    teams_config = mesh_config.project.connectors["teams"]
    ingress = TeamsBotIngress(
        connector_id="teams-bot-listener",
        project_id=mesh_config.project.project_id,
        state_root=state_root,
        message_store=message_store,
        journal=journal,
        connector_config=teams_config,
        project_config=mesh_config.project,
        connector_outbox=connector_outbox,
    )

    ingress.receive_activity(
        {
            "type": "message",
            "id": "activity/work-queue-capture",
            "serviceUrl": "https://smba.trafficmanager.net/uk/",
            "text": "<at>all-agents</at> Please inspect the work queue design.",
            "from": {"id": "raw-user-id", "name": "Nich"},
            "conversation": {"id": "raw-conversation-id"},
            "channelData": {
                "team": {"id": teams_config.team_id},
                "channel": {
                    "id": teams_config.channels["all-agents"].channel_id,
                    "name": "all-agents",
                },
            },
        }
    )

    events = journal.read_all()
    event_types = [event["event_type"] for event in events]
    assert event_types.index("queue_item_created") < event_types.index("message_accepted")
    message = message_store.claim_next(
        "business-analyst",
        "agentic-mesh-dev.business-analyst.1",
    )
    assert message is not None
    assert message.payload["queue_item_id"].startswith("queue-")
    assert message.payload["source_anchor"]["source_anchor_ref"].startswith("source:")
    rendered = json.dumps(message.payload["source_anchor"])
    assert "raw-user-id" not in rendered
    assert "raw-conversation-id" not in rendered


def test_targeted_delivery_slice_request_enters_lifecycle_queue(
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
    work_queue = FileWorkQueueStore(state_root, mesh_config.project.project_id, journal)
    teams_config = mesh_config.project.connectors["teams"]
    ingress = TeamsBotIngress(
        connector_id="teams-bot-listener",
        project_id=mesh_config.project.project_id,
        state_root=state_root,
        message_store=message_store,
        journal=journal,
        connector_config=teams_config,
        project_config=mesh_config.project,
        connector_outbox=connector_outbox,
        work_queue=work_queue,
    )

    result = ingress.receive_activity(
        {
            "type": "message",
            "id": "activity/mermaid-slice",
            "serviceUrl": "https://smba.trafficmanager.net/uk/",
            "text": (
                "<at>AM-Delivery Manager</at> Please create and run a small "
                "implementation slice for a Mermaid lifecycle chart export."
            ),
            "from": {"id": "user-1", "name": "Nich"},
            "conversation": {"id": "conversation-1"},
            "channelData": {
                "team": {"id": teams_config.team_id},
                "channel": {
                    "id": teams_config.channels["all-agents"].channel_id,
                    "name": "all-agents",
                },
            },
            "entities": [
                {
                    "type": "mention",
                    "text": "<at>AM-Delivery Manager</at>",
                    "mentioned": {"name": "AM-Delivery Manager"},
                }
            ],
        }
    )

    assert result["routed"] is True
    assert result["target_roles"] == ["business-analyst"]
    assert message_store.pending_count("delivery-manager") == 0
    assert message_store.pending_count("business-analyst") == 1
    message = message_store.claim_next(
        "business-analyst",
        "agentic-mesh-dev.business-analyst.1",
    )
    assert message is not None
    assert message.type == "sponsor_intake.requested"
    assert message.payload["work_item_type"] == "slice"
    assert message.payload["queue_item_id"].startswith("queue-")
    queue_item = work_queue.get(message.payload["queue_item_id"])
    assert queue_item is not None
    assert queue_item.metadata["intake"] == "targeted_delivery_slice_request"

    acknowledgement = connector_outbox.claim_next("all-agents", "test-connector")
    assert acknowledgement is not None
    assert acknowledgement.type == "sponsor_directive.acknowledged"
    assert acknowledgement.payload["intake_mode"] == "queued_sponsor_intake"
    assert acknowledgement.payload["teams_reply_to_activity_id"] == "activity/mermaid-slice"
    rendered = BotFrameworkTeamsConnectorAdapter._render_text(acknowledgement)
    assert "Agentic Mesh queued" in rendered
    assert "Created lifecycle work item" in rendered
    assert "direct role-only instruction" in rendered


def test_cross_connector_duplicate_intake_is_ignored(
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
    work_queue = FileWorkQueueStore(state_root, mesh_config.project.project_id, journal)
    teams_config = mesh_config.project.connectors["teams"]

    def ingress(connector_id: str) -> TeamsBotIngress:
        return TeamsBotIngress(
            connector_id=connector_id,
            project_id=mesh_config.project.project_id,
            state_root=state_root,
            message_store=message_store,
            journal=journal,
            connector_config=teams_config,
            project_config=mesh_config.project,
            connector_outbox=connector_outbox,
            work_queue=work_queue,
        )

    activity = {
        "type": "message",
        "id": "activity/duplicate",
        "serviceUrl": "https://smba.trafficmanager.net/uk/",
        "text": "<at>AM-Delivery Manager</at> Please create and run a small slice.",
        "from": {"id": "user-1", "name": "Nich"},
        "conversation": {"id": "conversation-1"},
        "channelData": {
            "team": {"id": teams_config.team_id},
            "channel": {
                "id": teams_config.channels["all-agents"].channel_id,
                "name": "all-agents",
            },
        },
        "entities": [
            {
                "type": "mention",
                "text": "<at>AM-Delivery Manager</at>",
                "mentioned": {"name": "AM-Delivery Manager"},
            }
        ],
    }

    first = ingress("teams-bot-listener").receive_activity(activity)
    second = ingress("teams-graph-ingress").receive_activity(activity)

    assert first["routed"] is True
    assert second == {"status": "accepted", "activity_id": "activity_duplicate"}
    assert len(work_queue.list_items()) == 1
    assert message_store.pending_count("business-analyst") == 1
    assert connector_outbox.pending_count("all-agents") == 1
    ignored = [
        event
        for event in journal.read_all()
        if event["event_type"] == "teams_channel_message_ignored"
    ]
    assert ignored[-1]["reason"] == "duplicate_source_already_queued"


def test_teams_ingress_informational_message_remains_unqueued(
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
    work_queue = FileWorkQueueStore(state_root, mesh_config.project.project_id, journal)
    teams_config = mesh_config.project.connectors["teams"]
    ingress = TeamsBotIngress(
        connector_id="teams-bot-listener",
        project_id=mesh_config.project.project_id,
        state_root=state_root,
        message_store=message_store,
        journal=journal,
        connector_config=teams_config,
        project_config=mesh_config.project,
        connector_outbox=connector_outbox,
        work_queue=work_queue,
    )

    result = ingress.receive_activity(
        {
            "type": "message",
            "id": "activity/informational",
            "serviceUrl": "https://smba.trafficmanager.net/uk/",
            "text": "FYI: the previous queue command completed.",
            "from": {"id": "user-1", "name": "Nich"},
            "conversation": {"id": "conversation-1"},
            "channelData": {
                "team": {"id": teams_config.team_id},
                "channel": {
                    "id": teams_config.channels["all-agents"].channel_id,
                    "name": "all-agents",
                },
            },
        }
    )

    assert result == {"status": "accepted", "activity_id": "activity_informational"}
    assert work_queue.list_items() == []
    assert all(
        message_store.pending_count(role_id) == 0
        for role_id in mesh_config.project.roles
    )
    assert connector_outbox.pending_count("all-agents") == 0
    ignored = [
        event
        for event in journal.read_all()
        if event["event_type"] == "teams_channel_message_ignored"
    ]
    assert ignored[0]["reason"] == "missing_all_agents_or_role_mention"


def test_queue_clarification_request_targets_source_anchor() -> None:
    mesh_config = load_mesh_config(Path.cwd())
    source_message = Message.create(
        role_id="product-manager",
        message_type="sdlc.product_definition",
        payload={
            "title": "Work Queue V0",
            "summary": "Clarify queue source routing.",
            "work_item_id": "work-queue-v0",
            "work_item_type": "slice",
            "lifecycle_state": "product_definition",
            "queue_item_id": "queue-clarify",
            "source_anchor": {
                "connector_type": "teams",
                "connector_id": "teams-bot-listener",
                "source_scope": "sponsor-requests",
                "source_anchor_ref": "source:clarify",
                "display_label": "Nich in sponsor-requests",
                "received_at": "2026-06-04T10:00:00+00:00",
            },
        },
        source="work-queue:queue-clarify",
    )
    gate = FlowGate(
        gate_id="queue_clarification_response",
        type="human_response",
        response_type="multiline_text",
        prompt="What outcome should this queue item produce?",
        requested_from="requester",
        channel="all-agents",
    )
    flow_state = FlowState(
        state_id="product_definition",
        owner_role="product-manager",
        purpose="Define product intent.",
        artifact_path="work-items/{work_item_id}/20-product-definition.md",
        handoffs={},
        gates=[gate],
    )

    request = build_human_response_request(
        gate=gate,
        response_type=None,
        source_message=source_message,
        source_instance=mesh_config.instances["agentic-mesh-dev.product-manager.1"],
        flow_state=flow_state,
    )

    assert request.channel == "sponsor-requests"
    assert request.channel != "all-agents"
    assert request.payload["queue_item_id"] == "queue-clarify"
    assert request.payload["source_anchor"]["source_anchor_ref"] == "source:clarify"


def test_teams_ingress_routes_named_role_mention_in_all_agents_channel(
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
    teams_config = mesh_config.project.connectors["teams"]
    ingress = TeamsBotIngress(
        connector_id="teams-bot-listener",
        project_id=mesh_config.project.project_id,
        state_root=state_root,
        message_store=message_store,
        journal=journal,
        connector_config=teams_config,
        project_config=mesh_config.project,
        connector_outbox=connector_outbox,
    )

    result = ingress.receive_activity(
        {
            "type": "message",
            "id": "activity/product-manager",
            "serviceUrl": "https://smba.trafficmanager.net/uk/",
            "text": "<at>AM-Product Manager</at> Please refine the adoption story.",
            "entities": [
                {
                    "type": "mention",
                    "text": "<at>AM-Product Manager</at>",
                    "mentioned": {"id": "bot-product-manager", "name": "AM-Product Manager"},
                }
            ],
            "from": {"id": "user-1", "name": "Nich"},
            "conversation": {"id": "conversation-1"},
            "channelData": {
                "team": {"id": teams_config.team_id},
                "channel": {
                    "id": teams_config.channels["all-agents"].channel_id,
                    "name": "all-agents",
                },
            },
        }
    )

    assert result["routed"] is True
    assert result["target_roles"] == ["product-manager"]
    assert message_store.pending_count("product-manager") == 1
    assert all(
        message_store.pending_count(role_id) == 0
        for role_id in mesh_config.project.roles
        if role_id != "product-manager"
    )
    acknowledgement = connector_outbox.claim_next("all-agents", "test-connector")
    assert acknowledgement is not None
    assert acknowledgement.payload["role_id"] == "product-manager"
    assert acknowledgement.payload["role_count"] == 1
    assert acknowledgement.payload["target_roles"] == ["product-manager"]

    message = message_store.claim_next(
        "product-manager",
        "agentic-mesh-dev.product-manager.1",
    )
    assert message is not None
    assert message.type == "sponsor_directive.requested"
    assert message.payload["work_mode"] == "direct_targeted"
    assert message.payload["requested_roles"] == ["product-manager"]
    assert message.payload["target_role"] == "product-manager"
    assert message.payload["output_path"] == "documents/analysis/product-manager.md"

    event_types = [event["event_type"] for event in journal.read_all()]
    assert "teams_targeted_directive_routed" in event_types
    assert "teams_all_agents_directive_routed" not in event_types


def test_teams_ingress_routes_personal_message_to_recipient_role(
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
    work_queue = FileWorkQueueStore(state_root, mesh_config.project.project_id, journal)
    teams_config = mesh_config.project.connectors["teams"]
    ingress = TeamsBotIngress(
        connector_id="teams-bot-listener",
        project_id=mesh_config.project.project_id,
        state_root=state_root,
        message_store=message_store,
        journal=journal,
        connector_config=teams_config,
        project_config=mesh_config.project,
        secrets=_StaticSecrets(),  # type: ignore[arg-type]
        connector_outbox=connector_outbox,
        work_queue=work_queue,
    )

    result = ingress.receive_activity(
        {
            "type": "message",
            "id": "activity-product-manager-dm",
            "serviceUrl": "https://smba.trafficmanager.net/uk/",
            "text": "Can you explain the product scope trade-offs in role?",
            "from": {"id": "user-1", "name": "Nich"},
            "recipient": {
                "id": "28:value-for-teams-bot-product-manager-app-id",
                "name": "AM-Product Manager",
            },
            "conversation": {
                "id": "personal-conversation-1",
                "conversationType": "personal",
            },
        }
    )

    assert result["status"] == "accepted"
    assert result["routed"] is True
    assert result["target_roles"] == ["product-manager"]
    message = message_store.claim_next(
        "product-manager",
        "agentic-mesh-dev.product-manager.1",
    )
    assert message is not None
    assert message.type == "sponsor_directive.requested"
    assert message.payload["work_mode"] == "direct_targeted"
    assert message.payload["source_channel"] == "dm"
    assert message.payload["teams_conversation_id"] == "personal-conversation-1"
    assert message.payload["target_role"] == "product-manager"
    acknowledgement = connector_outbox.claim_next(
        "all-agents",
        "teams-bot-connector",
    )
    assert acknowledgement is not None
    assert acknowledgement.payload["source_channel"] == "dm"
    assert acknowledgement.payload["role_id"] == "product-manager"
    assert acknowledgement.payload["teams_conversation_id"] == (
        "personal-conversation-1"
    )
    event_types = [event["event_type"] for event in journal.read_all()]
    assert "teams_targeted_directive_routed" in event_types


def test_teams_ingress_journals_unmapped_channel_message(
    tmp_path: Path,
) -> None:
    mesh_config = load_mesh_config(Path.cwd())
    state_root = tmp_path / "state"
    journal = EventJournal(state_root, mesh_config.project.project_id)
    message_store = FileMessageStore(state_root, mesh_config.project.project_id, journal)
    _seed_release_approval_context(state_root, project_id=mesh_config.project.project_id)
    ingress = TeamsBotIngress(
        connector_id="teams-bot-listener",
        project_id=mesh_config.project.project_id,
        state_root=state_root,
        message_store=message_store,
        journal=journal,
        connector_config=mesh_config.project.connectors["teams"],
        project_config=mesh_config.project,
    )

    result = ingress.receive_activity(
        {
            "type": "message",
            "id": "activity/unmapped",
            "text": "This should not disappear silently.",
            "from": {"id": "user-1", "name": "Nich"},
            "conversation": {"id": "conversation-unknown"},
            "channelData": {
                "channel": {"id": "unknown-channel", "name": "unknown"},
            },
        }
    )

    assert result == {"status": "accepted", "activity_id": "activity_unmapped"}
    assert message_store.pending_count("business-analyst") == 0
    ignored = [
        event
        for event in journal.read_all()
        if event["event_type"] == "teams_channel_message_ignored"
    ]
    assert ignored[0]["reason"] == "unmapped_channel"


def test_graph_teams_channel_ingress_routes_all_agents_message(
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
    teams_config = mesh_config.project.connectors["teams"]
    ingress = GraphTeamsChannelIngressAdapter(
        connector_id="teams-graph-ingress",
        project_id=mesh_config.project.project_id,
        state_root=state_root,
        connector_config=teams_config,
        message_store=message_store,
        journal=journal,
        project_config=mesh_config.project,
        token="token",
        connector_outbox=connector_outbox,
    )
    ingress._list_channel_messages = lambda channel, max_messages: [
        {
            "id": "1780489884072",
            "createdDateTime": "2026-06-03T12:31:24.072Z",
            "subject": "Adopt this project",
            "body": {
                "contentType": "html",
                "content": (
                    "<div><at id=\"0\">all-agents</at> Start an adoption "
                    "process for Agentic Mesh.</div>"
                ),
            },
            "from": {"user": {"id": "user-1", "displayName": "Nich"}},
            "mentions": [{"id": 0, "mentionText": "all-agents"}],
            "webUrl": "https://teams.example/message/1780489884072",
        }
    ]

    result = ingress.process_once("all-agents", max_messages=5)

    role_count = len(mesh_config.project.roles)
    assert result == {"routed": role_count, "skipped": 0, "seen": 1}
    assert connector_outbox.pending_count("all-agents") == 1
    assert message_store.pending_count("business-analyst") == 1
    message = message_store.claim_next(
        "business-analyst",
        "agentic-mesh-dev.business-analyst.1",
    )
    assert message is not None
    assert message.type == "sponsor_directive.requested"
    assert message.source == "teams:teams-graph-ingress:all-agents"
    assert message.payload["work_item_type"] == "directive"
    assert message.payload["source_channel"] == "all-agents"
    assert message.payload["teams_activity_id"] == "1780489884072"
    assert message.payload["teams_from_name"] == "Nich"

    event_types = [event["event_type"] for event in journal.read_all()]
    assert "teams_graph_channel_message_received" in event_types
    assert "teams_all_agents_directive_routed" in event_types


def test_graph_teams_channel_ingress_routes_named_role_mention(
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
    teams_config = mesh_config.project.connectors["teams"]
    ingress = GraphTeamsChannelIngressAdapter(
        connector_id="teams-graph-ingress",
        project_id=mesh_config.project.project_id,
        state_root=state_root,
        connector_config=teams_config,
        message_store=message_store,
        journal=journal,
        project_config=mesh_config.project,
        token="token",
        connector_outbox=connector_outbox,
    )
    ingress._list_channel_messages = lambda channel, max_messages: [
        {
            "id": "role-mention-1",
            "createdDateTime": "2026-06-03T12:31:24.072Z",
            "body": {
                "contentType": "html",
                "content": (
                    "<div><at id=\"0\">AM-Product Manager</at> Please refine "
                    "the adoption story.</div>"
                ),
            },
            "from": {"user": {"id": "user-1", "displayName": "Nich"}},
            "mentions": [{"id": 0, "mentionText": "AM-Product Manager"}],
            "webUrl": "https://teams.example/message/role-mention-1",
        }
    ]

    result = ingress.process_once("all-agents", max_messages=5)

    assert result == {"routed": 1, "skipped": 0, "seen": 1}
    assert message_store.pending_count("product-manager") == 1
    assert all(
        message_store.pending_count(role_id) == 0
        for role_id in mesh_config.project.roles
        if role_id != "product-manager"
    )
    acknowledgement = connector_outbox.claim_next("all-agents", "test-connector")
    assert acknowledgement is not None
    assert acknowledgement.payload["role_id"] == "product-manager"
    message = message_store.claim_next(
        "product-manager",
        "agentic-mesh-dev.product-manager.1",
    )
    assert message is not None
    assert message.source == "teams:teams-graph-ingress:all-agents"
    assert message.payload["work_mode"] == "direct_targeted"
    assert message.payload["teams_activity_id"] == "role-mention-1"
    assert message.payload["teams_from_name"] == "Nich"

    event_types = [event["event_type"] for event in journal.read_all()]
    assert "teams_graph_channel_message_received" in event_types
    assert "teams_targeted_directive_routed" in event_types
    assert "teams_all_agents_directive_routed" not in event_types


def test_graph_teams_channel_ingress_routes_leading_role_address_without_metadata(
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
    teams_config = mesh_config.project.connectors["teams"]
    ingress = GraphTeamsChannelIngressAdapter(
        connector_id="teams-graph-ingress",
        project_id=mesh_config.project.project_id,
        state_root=state_root,
        connector_config=teams_config,
        message_store=message_store,
        journal=journal,
        project_config=mesh_config.project,
        token="token",
        connector_outbox=connector_outbox,
    )
    ingress._list_channel_messages = lambda channel, max_messages: [
        {
            "id": "plain-role-address-1",
            "createdDateTime": "2026-06-04T10:47:32.895Z",
            "subject": "Mermaid flow diagram",
            "body": {
                "contentType": "html",
                "content": (
                    "<div>@AM-Delivery Manager Please create and run a small "
                    "implementation slice to add a CLI command.</div>"
                ),
            },
            "from": {"user": {"id": "user-1", "displayName": "Nich"}},
            "mentions": [],
            "webUrl": "https://teams.example/message/plain-role-address-1",
        }
    ]

    result = ingress.process_once("all-agents", max_messages=5)

    assert result == {"routed": 1, "skipped": 0, "seen": 1}
    assert message_store.pending_count("delivery-manager") == 0
    assert message_store.pending_count("business-analyst") == 1
    assert all(
        message_store.pending_count(role_id) == 0
        for role_id in mesh_config.project.roles
        if role_id not in {"business-analyst", "delivery-manager"}
    )
    acknowledgement = connector_outbox.claim_next("all-agents", "test-connector")
    assert acknowledgement is not None
    assert acknowledgement.payload["role_id"] == "business-analyst"
    assert acknowledgement.payload["intake_mode"] == "queued_sponsor_intake"
    message = message_store.claim_next(
        "business-analyst",
        "agentic-mesh-dev.business-analyst.1",
    )
    assert message is not None
    assert message.type == "sponsor_intake.requested"
    assert message.payload["work_item_type"] == "slice"
    assert message.payload["lifecycle_state"] == "business_analysis"

    event_types = [event["event_type"] for event in journal.read_all()]
    assert "teams_channel_message_routed" in event_types
    assert "teams_targeted_directive_routed" not in event_types
    assert "teams_all_agents_directive_routed" not in event_types


def test_graph_teams_channel_ingress_skips_all_agents_acknowledgement_echo(
    tmp_path: Path,
) -> None:
    mesh_config = load_mesh_config(Path.cwd())
    state_root = tmp_path / "state"
    journal = EventJournal(state_root, mesh_config.project.project_id)
    message_store = FileMessageStore(state_root, mesh_config.project.project_id, journal)
    teams_config = mesh_config.project.connectors["teams"]
    ingress = GraphTeamsChannelIngressAdapter(
        connector_id="teams-graph-ingress",
        project_id=mesh_config.project.project_id,
        state_root=state_root,
        connector_config=teams_config,
        message_store=message_store,
        journal=journal,
        project_config=mesh_config.project,
        token="token",
    )
    ingress._list_channel_messages = lambda channel, max_messages: [
        {
            "id": "ack-1",
            "createdDateTime": "2026-06-03T12:32:24.072Z",
            "body": {
                "contentType": "html",
                "content": (
                    "<p><strong>Agentic Mesh received:</strong> All-agents "
                    "directive received. Created direct work item work-1.</p>"
                ),
            },
            "from": {
                "application": {
                    "id": "agentic-mesh-bot",
                    "displayName": "AM-Delivery Manager",
                },
            },
            "mentions": [],
        }
    ]

    result = ingress.process_once("all-agents", max_messages=5)

    assert result == {"routed": 0, "skipped": 1, "seen": 1}
    assert message_store.pending_count("business-analyst") == 0
    skipped = [
        event
        for event in journal.read_all()
        if event["event_type"] == "teams_graph_channel_message_skipped"
    ]
    assert skipped[0]["reason"] == "connector_echo"


def test_graph_teams_channel_ingress_requires_real_all_agents_mention(
    tmp_path: Path,
) -> None:
    mesh_config = load_mesh_config(Path.cwd())
    state_root = tmp_path / "state"
    journal = EventJournal(state_root, mesh_config.project.project_id)
    message_store = FileMessageStore(state_root, mesh_config.project.project_id, journal)
    teams_config = mesh_config.project.connectors["teams"]
    ingress = GraphTeamsChannelIngressAdapter(
        connector_id="teams-graph-ingress",
        project_id=mesh_config.project.project_id,
        state_root=state_root,
        connector_config=teams_config,
        message_store=message_store,
        journal=journal,
        project_config=mesh_config.project,
        token="token",
    )
    ingress._list_channel_messages = lambda channel, max_messages: [
        {
            "id": "plain-1",
            "createdDateTime": "2026-06-03T12:32:24.072Z",
            "body": {
                "contentType": "html",
                "content": "<p>all-agents should have received the work item.</p>",
            },
            "from": {"user": {"id": "user-1", "displayName": "Nich"}},
            "mentions": [],
        }
    ]

    result = ingress.process_once("all-agents", max_messages=5)

    assert result == {"routed": 0, "skipped": 1, "seen": 1}
    assert message_store.pending_count("business-analyst") == 0
    skipped = [
        event
        for event in journal.read_all()
        if event["event_type"] == "teams_graph_channel_message_skipped"
    ]
    assert skipped[0]["reason"] == "missing_channel_mention"


def test_bot_known_parent_threaded_reply_binds_before_direct_work(
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
    work_queue = FileWorkQueueStore(state_root, mesh_config.project.project_id, journal)
    teams_config = mesh_config.project.connectors["teams"]
    ingress = TeamsBotIngress(
        connector_id="teams-bot-listener",
        project_id=mesh_config.project.project_id,
        state_root=state_root,
        message_store=message_store,
        journal=journal,
        connector_config=teams_config,
        project_config=mesh_config.project,
        connector_outbox=connector_outbox,
        work_queue=work_queue,
    )
    channel = teams_config.channels["engineering"]

    root = ingress.receive_activity(
        {
            "type": "message",
            "id": "activity-parent-1",
            "serviceUrl": "https://smba.trafficmanager.net/uk/",
            "text": "Please build the parent feature.",
            "from": {"id": "user-1", "name": "Nich"},
            "conversation": {"id": "conversation-1"},
            "channelData": {
                "team": {"id": teams_config.team_id},
                "channel": {"id": channel.channel_id, "name": channel.name},
            },
        }
    )
    reply = ingress.receive_activity(
        {
            "type": "message",
            "id": "activity-reply-1",
            "replyToId": "activity-parent-1",
            "serviceUrl": "https://smba.trafficmanager.net/uk/",
            "text": "<at>AM-Product Manager</at> Please include this context.",
            "entities": [
                {
                    "type": "mention",
                    "text": "<at>AM-Product Manager</at>",
                    "mentioned": {"id": "bot-product-manager", "name": "AM-Product Manager"},
                }
            ],
            "from": {"id": "user-1", "name": "Nich"},
            "conversation": {"id": "conversation-1"},
            "channelData": {
                "team": {"id": teams_config.team_id},
                "channel": {"id": channel.channel_id, "name": channel.name},
            },
        }
    )

    assert root["routed"] is True
    assert reply["routed"] is True
    assert reply["target_role"] == "business-analyst"
    assert len(work_queue.list_items()) == 1
    assert message_store.pending_count("product-manager") == 0
    assert connector_outbox.pending_count("engineering") == 2
    contexts = ingress.threaded_contexts.list_contexts(root["work_item_id"])
    assert len(contexts) == 1
    assert contexts[0].parent_work_item_id == root["work_item_id"]
    assert contexts[0].mentioned_roles == ("product-manager",)
    assert contexts[0].attention_state == "pending"

    events = journal.read_all()
    event_types = [event["event_type"] for event in events]
    assert "threaded_context.captured" in event_types
    assert "threaded_context.owner_attention.routed" in event_types
    assert "teams_channel_message_routed" in event_types
    assert len([event for event in events if event["event_type"] == "queue_item_created"]) == 1


def test_threaded_reply_to_active_clarification_submits_human_response(
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
    work_queue = FileWorkQueueStore(state_root, mesh_config.project.project_id, journal)
    teams_config = mesh_config.project.connectors["teams"]
    ingress = TeamsBotIngress(
        connector_id="teams-bot-listener",
        project_id=mesh_config.project.project_id,
        state_root=state_root,
        message_store=message_store,
        journal=journal,
        connector_config=teams_config,
        project_config=mesh_config.project,
        connector_outbox=connector_outbox,
        work_queue=work_queue,
    )
    channel = teams_config.channels["business-analysis"]
    root = ingress.receive_activity(
        {
            "type": "message",
            "id": "activity-clarification-parent",
            "serviceUrl": "https://smba.trafficmanager.net/uk/",
            "text": "Please define the gateway bot.",
            "entities": [],
            "from": {"id": "user-1", "name": "Nich"},
            "conversation": {"id": "conversation-1"},
            "channelData": {
                "team": {"id": teams_config.team_id},
                "channel": {"id": channel.channel_id, "name": channel.name},
            },
        }
    )
    assert root["routed"] is True
    work_item_id = root["work_item_id"]
    store = FileHumanGateRequestStore(state_root, mesh_config.project.project_id)
    request, _ = store.ensure_request(
        work_item_id=work_item_id,
        work_item_type="slice",
        lifecycle_state="business_analysis",
        gate=FlowGate(
            gate_id="sponsor_clarification_response",
            type="human_response",
            response_type="multiline_text",
            prompt="Please answer the open sponsor questions.",
            requested_from="sponsor",
            channel="business-analysis",
        ),
    )
    store.mark_enqueue_succeeded(
        request.response_request_id,
        connector_message_id="conn-msg-clarification",
    )

    reply = ingress.receive_activity(
        {
            "type": "message",
            "id": "activity-clarification-reply",
            "replyToId": "activity-clarification-parent",
            "serviceUrl": "https://smba.trafficmanager.net/uk/",
            "text": "Use one gateway bot named agentic-mesh and support Teams DMs.",
            "from": {"id": "user-1", "name": "Nich"},
            "conversation": {"id": "conversation-1"},
            "channelData": {
                "team": {"id": teams_config.team_id},
                "channel": {"id": channel.channel_id, "name": channel.name},
            },
        }
    )

    assert reply["status"] == "accepted"
    assert reply["message_id"].startswith("msg-")
    received = message_store.claim_next("business-analyst", "test-ba")
    assert received is not None
    while received.type != "human_response.received":
        message_store.complete(received.claimed("test-ba"), "test-drain")
        received = message_store.claim_next("business-analyst", "test-ba")
        assert received is not None
    assert received.payload["work_item_id"] == work_item_id
    assert received.payload["gate_id"] == "sponsor_clarification_response"
    assert received.payload["response_value"] == (
        "Use one gateway bot named agentic-mesh and support Teams DMs."
    )
    stored = store.read(request.response_request_id)
    assert stored is not None
    assert stored.status == "completed"
    event_types = [event["event_type"] for event in journal.read_all()]
    assert "threaded_context.human_response_submitted" in event_types


def test_explicit_linked_new_work_threaded_reply_uses_governed_intake(
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
    work_queue = FileWorkQueueStore(state_root, mesh_config.project.project_id, journal)
    teams_config = mesh_config.project.connectors["teams"]
    channel = teams_config.channels["engineering"]
    ingress = TeamsBotIngress(
        connector_id="teams-bot-listener",
        project_id=mesh_config.project.project_id,
        state_root=state_root,
        message_store=message_store,
        journal=journal,
        connector_config=teams_config,
        project_config=mesh_config.project,
        connector_outbox=connector_outbox,
        work_queue=work_queue,
    )

    root = ingress.receive_activity(
        {
            "type": "message",
            "id": "activity-linked-parent",
            "text": "Please build the parent feature.",
            "from": {"id": "user-1", "name": "Nich"},
            "conversation": {"id": "conversation-1"},
            "channelData": {
                "team": {"id": teams_config.team_id},
                "channel": {"id": channel.channel_id, "name": channel.name},
            },
        }
    )
    linked = ingress.receive_activity(
        {
            "type": "message",
            "id": "activity-linked-reply",
            "replyToId": "activity-linked-parent",
            "text": "Please create a new slice for this follow-up.",
            "from": {"id": "user-1", "name": "Nich"},
            "conversation": {"id": "conversation-1"},
            "channelData": {
                "team": {"id": teams_config.team_id},
                "channel": {"id": channel.channel_id, "name": channel.name},
            },
        }
    )

    assert root["routed"] is True
    assert linked["status"] == "accepted"
    assert len(work_queue.list_items()) == 2
    parent_contexts = ingress.threaded_contexts.list_contexts(root["work_item_id"])
    assert len(parent_contexts) == 1
    assert parent_contexts[0].context_kind == "linked_new_work"
    assert parent_contexts[0].reason == "explicit new-work wording was detected"
    messages = [
        message_store.claim_next(
            "business-analyst",
            "agentic-mesh-dev.business-analyst.1",
        ),
        message_store.claim_next(
            "business-analyst",
            "agentic-mesh-dev.business-analyst.1",
        ),
    ]
    linked_message = [
        message
        for message in messages
        if message is not None and message.payload["parent_work_item_id"]
    ][0]
    assert linked_message.type == "sponsor_intake.requested"
    linked_item = work_queue.get(linked_message.payload["queue_item_id"])
    assert linked_item is not None
    assert linked_item.metadata["intake"] == "linked_new_work_from_threaded_context"
    assert linked_item.metadata["parent_work_item_id"] == root["work_item_id"]
    assert (
        linked_item.metadata["parent_threaded_context_id"]
        == parent_contexts[0].context_id
    )


def test_ambiguous_known_parent_threaded_reply_stays_parent_context(
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
    work_queue = FileWorkQueueStore(state_root, mesh_config.project.project_id, journal)
    teams_config = mesh_config.project.connectors["teams"]
    channel = teams_config.channels["engineering"]
    ingress = TeamsBotIngress(
        connector_id="teams-bot-listener",
        project_id=mesh_config.project.project_id,
        state_root=state_root,
        message_store=message_store,
        journal=journal,
        connector_config=teams_config,
        project_config=mesh_config.project,
        connector_outbox=connector_outbox,
        work_queue=work_queue,
    )

    root = ingress.receive_activity(
        {
            "type": "message",
            "id": "activity-ambiguous-parent",
            "text": "Please build the parent feature.",
            "from": {"id": "user-1", "name": "Nich"},
            "conversation": {"id": "conversation-1"},
            "channelData": {
                "team": {"id": teams_config.team_id},
                "channel": {"id": channel.channel_id, "name": channel.name},
            },
        }
    )
    reply = ingress.receive_activity(
        {
            "type": "message",
            "id": "activity-ambiguous-reply",
            "replyToId": "activity-ambiguous-parent",
            "text": "<at>AM-Product Manager</at> please handle this",
            "entities": [
                {
                    "type": "mention",
                    "text": "<at>AM-Product Manager</at>",
                    "mentioned": {"id": "bot-product-manager", "name": "AM-Product Manager"},
                }
            ],
            "from": {"id": "user-1", "name": "Nich"},
            "conversation": {"id": "conversation-1"},
            "channelData": {
                "team": {"id": teams_config.team_id},
                "channel": {"id": channel.channel_id, "name": channel.name},
            },
        }
    )

    assert root["routed"] is True
    assert reply["status"] == "accepted"
    assert len(work_queue.list_items()) == 1
    assert message_store.pending_count("product-manager") == 0
    assert message_store.pending_count("business-analyst") == 2
    contexts = ingress.threaded_contexts.list_contexts(root["work_item_id"])
    assert len(contexts) == 1
    assert contexts[0].context_kind == "parent_context"
    assert contexts[0].mentioned_roles == ("product-manager",)


def test_unknown_parent_threaded_reply_is_observable_without_binding(
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
    work_queue = FileWorkQueueStore(state_root, mesh_config.project.project_id, journal)
    teams_config = mesh_config.project.connectors["teams"]
    channel = teams_config.channels["engineering"]
    ingress = TeamsBotIngress(
        connector_id="teams-bot-listener",
        project_id=mesh_config.project.project_id,
        state_root=state_root,
        message_store=message_store,
        journal=journal,
        connector_config=teams_config,
        project_config=mesh_config.project,
        connector_outbox=connector_outbox,
        work_queue=work_queue,
    )

    result = ingress.receive_activity(
        {
            "type": "message",
            "id": "activity-unknown-reply",
            "replyToId": "activity-missing-parent",
            "text": "Please include this context.",
            "from": {"id": "user-1", "name": "Nich"},
            "conversation": {"id": "conversation-1"},
            "channelData": {
                "team": {"id": teams_config.team_id},
                "channel": {"id": channel.channel_id, "name": channel.name},
            },
        }
    )

    assert result == {"status": "accepted", "activity_id": "activity-unknown-reply"}
    assert ingress.threaded_contexts.list_contexts() == []
    assert work_queue.list_items() == []
    assert message_store.pending_count("business-analyst") == 0
    assert connector_outbox.pending_count("engineering") == 0
    parent_events = [
        event
        for event in journal.read_all()
        if event["event_type"] == "threaded_context.parent_not_verified"
    ]
    assert len(parent_events) == 1
    assert parent_events[0]["reason"] == "parent_not_verified"


def test_graph_known_parent_reply_uses_reply_fetching(
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
    teams_config = mesh_config.project.connectors["teams"]
    ingress = GraphTeamsChannelIngressAdapter(
        connector_id="teams-graph-ingress",
        project_id=mesh_config.project.project_id,
        state_root=state_root,
        connector_config=teams_config,
        message_store=message_store,
        journal=journal,
        project_config=mesh_config.project,
        token="token",
        connector_outbox=connector_outbox,
    )
    ingress._list_channel_messages = lambda channel, max_messages: [
        {
            "id": "graph-parent-1",
            "createdDateTime": "2026-06-03T12:31:24.072Z",
            "body": {"content": "<p>Please build the parent feature.</p>"},
            "from": {"user": {"id": "user-1", "displayName": "Nich"}},
            "mentions": [],
        }
    ]
    ingress._list_channel_replies = lambda channel, root_message_id, max_messages: [
        {
            "id": "graph-reply-1",
            "createdDateTime": "2026-06-03T12:32:24.072Z",
            "body": {"content": "<p>Please include this context.</p>"},
            "from": {"user": {"id": "user-1", "displayName": "Nich"}},
            "mentions": [],
        }
    ]

    result = ingress.process_once("engineering", max_messages=5)

    assert result["routed"] == 2
    first_message = message_store.claim_next(
        "business-analyst",
        "agentic-mesh-dev.business-analyst.1",
    )
    assert first_message is not None
    contexts = ingress.ingress.threaded_contexts.list_contexts(
        first_message.payload["work_item_id"]
    )
    assert len(contexts) == 1
    assert contexts[0].source_scope == "engineering"
    event_types = [event["event_type"] for event in journal.read_all()]
    assert "teams_graph_channel_message_received" in event_types
    assert "threaded_context.captured" in event_types


def test_bot_and_graph_duplicate_threaded_reply_converges_to_one_context(
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
    work_queue = FileWorkQueueStore(state_root, mesh_config.project.project_id, journal)
    teams_config = mesh_config.project.connectors["teams"]
    channel = teams_config.channels["engineering"]
    bot = TeamsBotIngress(
        connector_id="teams-bot-listener",
        project_id=mesh_config.project.project_id,
        state_root=state_root,
        message_store=message_store,
        journal=journal,
        connector_config=teams_config,
        project_config=mesh_config.project,
        connector_outbox=connector_outbox,
        work_queue=work_queue,
    )
    bot.receive_activity(
        {
            "type": "message",
            "id": "cross-parent-1",
            "text": "Please build the parent feature.",
            "from": {"id": "user-1", "name": "Nich"},
            "conversation": {"id": "conversation-1"},
            "channelData": {
                "team": {"id": teams_config.team_id},
                "channel": {"id": channel.channel_id, "name": channel.name},
            },
        }
    )
    bot_reply = bot.receive_activity(
        {
            "type": "message",
            "id": "cross-reply-1",
            "replyToId": "cross-parent-1",
            "text": "Please include this context.",
            "from": {"id": "user-1", "name": "Nich"},
            "conversation": {"id": "conversation-1"},
            "channelData": {
                "team": {"id": teams_config.team_id},
                "channel": {"id": channel.channel_id, "name": channel.name},
            },
        }
    )
    graph = GraphTeamsChannelIngressAdapter(
        connector_id="teams-graph-ingress",
        project_id=mesh_config.project.project_id,
        state_root=state_root,
        connector_config=teams_config,
        message_store=message_store,
        journal=journal,
        project_config=mesh_config.project,
        token="token",
        connector_outbox=connector_outbox,
    )
    graph._list_channel_messages = lambda channel_name, max_messages: [
        {
            "id": "cross-parent-1",
            "createdDateTime": "2026-06-03T12:31:24.072Z",
            "body": {"content": "<p>Please build the parent feature.</p>"},
            "from": {"user": {"id": "user-1", "displayName": "Nich"}},
            "mentions": [],
        }
    ]
    graph._list_channel_replies = lambda channel_name, root_message_id, max_messages: [
        {
            "id": "graph-cross-reply-different-delivery-id",
            "createdDateTime": "2026-06-03T12:32:24.072Z",
            "body": {"content": "<p>Please include this context.</p>"},
            "from": {"user": {"id": "user-1", "displayName": "Nich"}},
            "mentions": [],
        }
    ]

    graph_result = graph.process_once("engineering", max_messages=5)

    assert bot_reply["routed"] is True
    assert graph_result["routed"] == 0
    all_contexts = bot.threaded_contexts.list_contexts()
    assert len(all_contexts) == 1
    assert len(work_queue.list_items()) == 1
    event_types = [event["event_type"] for event in journal.read_all()]
    assert "threaded_context.duplicate_detected" in event_types
    assert len(
        [event for event in journal.read_all() if event["event_type"] == "threaded_context.receipt.queued"]
    ) == 1


def test_graph_teams_channel_ingress_cursor_suppresses_duplicates(
    tmp_path: Path,
) -> None:
    mesh_config = load_mesh_config(Path.cwd())
    state_root = tmp_path / "state"
    journal = EventJournal(state_root, mesh_config.project.project_id)
    message_store = FileMessageStore(state_root, mesh_config.project.project_id, journal)
    teams_config = mesh_config.project.connectors["teams"]
    ingress = GraphTeamsChannelIngressAdapter(
        connector_id="teams-graph-ingress",
        project_id=mesh_config.project.project_id,
        state_root=state_root,
        connector_config=teams_config,
        message_store=message_store,
        journal=journal,
        project_config=mesh_config.project,
        token="token",
    )
    graph_messages = [
        {
            "id": "message-1",
            "createdDateTime": "2026-06-03T12:31:24.072Z",
            "body": {
                "content": "<at id=\"0\">all-agents</at> Adopt the project.",
            },
            "from": {"user": {"id": "user-1", "displayName": "Nich"}},
            "mentions": [{"id": 0, "mentionText": "all-agents"}],
        }
    ]
    ingress._list_channel_messages = lambda channel, max_messages: graph_messages

    assert ingress.process_once("all-agents")["routed"] == len(mesh_config.project.roles)
    assert ingress.process_once("all-agents")["routed"] == 0
    assert message_store.pending_count("business-analyst") == 1


def test_load_graph_token_supports_client_credentials(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured = {}
    secret_file = tmp_path / "graph-client-secret"
    secret_file.write_text("secret-value", encoding="utf-8")

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        @staticmethod
        def read():
            return b'{"access_token":"graph-token"}'

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["body"] = req.data.decode("utf-8")
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("agentic_mesh.connectors.request.urlopen", fake_urlopen)

    token = load_graph_token(
        None,
        None,
        tenant_id="tenant-id",
        client_id="client-id",
        client_secret_file=secret_file,
    )

    assert token == "graph-token"
    assert "/tenant-id/oauth2/v2.0/token" in captured["url"]
    assert "grant_type=client_credentials" in captured["body"]
    assert "client_id=client-id" in captured["body"]
    assert "client_secret=secret-value" in captured["body"]


def test_teams_ingress_updates_original_human_response_card(tmp_path: Path) -> None:
    mesh_config = load_mesh_config(Path.cwd())
    state_root = tmp_path / "state"
    journal = EventJournal(state_root, mesh_config.project.project_id)
    message_store = FileMessageStore(state_root, mesh_config.project.project_id, journal)
    _seed_release_approval_context(state_root, project_id=mesh_config.project.project_id)
    ingress = TeamsBotIngress(
        connector_id="teams-bot-listener",
        project_id=mesh_config.project.project_id,
        state_root=state_root,
        message_store=message_store,
        journal=journal,
        connector_config=mesh_config.project.connectors["teams"],
        secrets=object(),
    )
    captured = {}

    def capture_update(**kwargs):
        captured.update(kwargs)
        return {"id": kwargs["activity_id"]}

    ingress._update_activity = capture_update

    result = ingress.receive_activity(
        {
            "type": "message",
            "id": "activity/456",
            "serviceUrl": "https://smba.trafficmanager.net/uk/tenant/",
            "from": {"id": "user-1", "name": "Nich"},
            "conversation": {"id": "conversation-1"},
            "replyToId": "activity-original",
            "value": {
                "action": "human_response.submit",
                "role_id": "release-manager",
                "work_item_id": "slice-release",
                "work_item_type": "slice",
                "lifecycle_state": "release_review",
                "gate_id": "release_decision_response",
                "response_request_id": "human-response-1",
                "response_value": "approved",
                "correlation_id": "corr-release",
            },
        }
    )

    assert result["statusCode"] == 200
    assert captured["service_url"] == "https://smba.trafficmanager.net/uk/tenant/"
    assert captured["conversation_id"] == "conversation-1"
    assert captured["activity_id"] == "activity-original"
    assert captured["role_id"] == "release-manager"
    actions = captured["card"]["actions"]
    assert [action["title"] for action in actions] == ["Approve"]
    assert actions[0]["isEnabled"] is False
    event_types = [event["event_type"] for event in journal.read_all()]
    assert "teams_bot_card_updated" in event_types


def test_bot_connector_selects_source_role_for_handoff_sender(monkeypatch) -> None:
    monkeypatch.delenv("AGENTIC_MESH_STATUS_BASE_URL", raising=False)
    monkeypatch.setenv(
        "AGENTIC_MESH_AUTH_ADMIN_URL",
        "http://controller.local/auth/credentials",
    )
    message = ConnectorMessage.create(
        channel="engineering",
        message_type="sdlc.handoff",
        payload={
            "source_role": "platform-engineer",
            "target_role": "engineering",
            "source_lifecycle_state": "platform_readiness",
            "target_lifecycle_state": "implementation",
            "work_item_id": "slice-bot-sender",
        },
        source="agentic-mesh-dev.platform-engineer.1",
    )
    connector = BotFrameworkTeamsConnectorAdapter.__new__(
        BotFrameworkTeamsConnectorAdapter
    )

    assert connector._sender_role(message) == "platform-engineer"
    rendered = connector._render_text(message)
    assert "platform_readiness" in rendered
    assert "implementation" in rendered
    assert "http://controller.local/work-items/slice-bot-sender" not in rendered

    monkeypatch.setenv("AGENTIC_MESH_STATUS_BASE_URL", "https://status.example.com")
    rendered_with_status_base = connector._render_text(message)
    assert (
        "https://status.example.com/work-items/slice-bot-sender"
        in rendered_with_status_base
    )


def test_bot_connector_attaches_human_response_card() -> None:
    mesh_config = load_mesh_config(Path.cwd())
    flow_state = mesh_config.project.flow.states["release_review"]
    gate = flow_state.gates[0]
    source_message = Message.create(
        role_id="release-manager",
        message_type="sdlc.release_review",
        payload={
            "title": "Release local runtime",
            "summary": "Confirm release readiness.",
            "work_item_id": "slice-release-card",
            "work_item_type": "slice",
            "lifecycle_state": "release_review",
        },
        source="test",
        correlation_id="corr-1234567890abcdef1234567890abcdef",
        trace_context={"trace_id": "1234567890abcdef1234567890abcdef"},
    )
    request = build_human_response_request(
        gate=gate,
        response_type=mesh_config.response_types[gate.response_type],
        source_message=source_message,
        source_instance=mesh_config.instances["agentic-mesh-dev.release-manager.1"],
        flow_state=flow_state,
    )
    connector = BotFrameworkTeamsConnectorAdapter.__new__(
        BotFrameworkTeamsConnectorAdapter
    )

    activity = connector._build_activity(request)

    assert activity["type"] == "message"
    assert activity["attachments"][0]["contentType"] == (
        "application/vnd.microsoft.card.adaptive"
    )
    card = activity["attachments"][0]["content"]
    assert card["type"] == "AdaptiveCard"
    assert [action["title"] for action in card["actions"]] == [
        "Approve",
        "Not Approve",
    ]
    assert card["actions"][0]["data"]["action"] == "human_response.submit"
    assert card["actions"][0]["data"]["response_value"] == "approved"
    assert "trace_context" not in card["actions"][0]["data"]


def test_bot_connector_renders_sponsor_directive_acknowledgement() -> None:
    message = ConnectorMessage.create(
        channel="all-agents",
        message_type="sponsor_directive.acknowledged",
        payload={
            "title": "Adopt this project",
            "work_item_id": "work-adoption",
            "git_branch": "codex/work-adoption-adopt-this-project",
            "role_id": "delivery-manager",
            "role_count": 13,
            "target_roles": ["business-analyst", "product-manager"],
        },
        source="test",
    )

    rendered = BotFrameworkTeamsConnectorAdapter._render_text(message)

    assert "Agentic Mesh received" in rendered
    assert "not a lifecycle handoff" in rendered
    assert "work-adoption" in rendered
    assert "codex/work-adoption-adopt-this-project" in rendered


def test_bot_connector_uses_thread_reply_url_when_source_reference_exists(
    tmp_path: Path,
) -> None:
    mesh_config = load_mesh_config(Path.cwd())
    state_root = tmp_path / "state"
    journal = EventJournal(state_root, mesh_config.project.project_id)
    outbox = FileConnectorOutbox(state_root, mesh_config.project.project_id, journal)
    connector = BotFrameworkTeamsConnectorAdapter(
        connector_id="teams-bot-connector",
        project_id=mesh_config.project.project_id,
        connector_config=mesh_config.project.connectors["teams"],
        outbox=outbox,
        journal=journal,
        secrets=object(),  # type: ignore[arg-type]
    )
    message = ConnectorMessage.create(
        channel="all-agents",
        message_type="sponsor_directive.acknowledged",
        payload={
            "title": "Queued slice",
            "work_item_id": "work-threaded",
            "role_id": "business-analyst",
            "role_count": 1,
            "target_roles": ["business-analyst"],
            "teams_service_url": "https://smba.trafficmanager.net/uk/",
            "teams_conversation_id": "19:conversation@thread.tacv2",
            "teams_reply_to_activity_id": "1780500000000",
        },
        source="test",
    )

    assert connector._thread_reply_url(message) == (
        "https://smba.trafficmanager.net/uk/v3/conversations/"
        "19%3Aconversation%40thread.tacv2/activities/1780500000000"
    )


def test_bot_connector_thread_reply_body_uses_role_bot_identity(
    monkeypatch,
    tmp_path: Path,
) -> None:
    mesh_config = load_mesh_config(Path.cwd())
    state_root = tmp_path / "state"
    journal = EventJournal(state_root, mesh_config.project.project_id)
    outbox = FileConnectorOutbox(state_root, mesh_config.project.project_id, journal)
    class FakeSecrets:
        def get(self, ref):
            return {
                "teams-bot-delivery-manager-app-id": "delivery-app-id",
                "teams-bot-delivery-manager-secret": "delivery-secret",
            }[ref]

    connector = BotFrameworkTeamsConnectorAdapter(
        connector_id="teams-bot-connector",
        project_id=mesh_config.project.project_id,
        connector_config=mesh_config.project.connectors["teams"],
        outbox=outbox,
        journal=journal,
        secrets=FakeSecrets(),  # type: ignore[arg-type]
    )
    connector._bot_token = lambda app_id, app_secret: "bot-token"
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"id":"thread-reply-id"}'

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode("utf-8"))
        captured["authorization"] = req.headers["Authorization"]
        return FakeResponse()

    monkeypatch.setattr("agentic_mesh.connectors.request.urlopen", fake_urlopen)

    message = ConnectorMessage.create(
        channel="all-agents",
        message_type="sponsor_directive.acknowledged",
        payload={
            "title": "Queued slice",
            "work_item_id": "work-threaded",
            "role_id": "delivery-manager",
            "role_count": 1,
            "target_roles": ["delivery-manager"],
            "teams_service_url": "https://smba.trafficmanager.net/uk/",
            "teams_conversation_id": "19:conversation@thread.tacv2",
            "teams_reply_to_activity_id": "1780500000000",
        },
        source="test",
    )

    connector._post_message(message, "delivery-manager")

    assert captured["url"].endswith(
        "/v3/conversations/19%3Aconversation%40thread.tacv2/"
        "activities/1780500000000"
    )
    assert captured["authorization"] == "Bearer bot-token"
    assert captured["body"]["from"] == {
        "id": "delivery-app-id",
        "name": "AM-Delivery Manager",
        "role": "bot",
    }
    assert captured["body"]["replyToId"] == "1780500000000"
    assert captured["body"]["conversation"]["id"] == "19:conversation@thread.tacv2"
    assert captured["body"]["channelData"]["agenticMesh"]["senderRole"] == (
        "delivery-manager"
    )


def test_bot_connector_thread_reply_body_supports_direct_message_scope(
    monkeypatch,
    tmp_path: Path,
) -> None:
    mesh_config = load_mesh_config(Path.cwd())
    state_root = tmp_path / "state"
    journal = EventJournal(state_root, mesh_config.project.project_id)
    outbox = FileConnectorOutbox(state_root, mesh_config.project.project_id, journal)

    class FakeSecrets:
        def get(self, ref):
            return {
                "teams-bot-product-manager-app-id": "product-app-id",
                "teams-bot-product-manager-secret": "product-secret",
            }[ref]

    connector = BotFrameworkTeamsConnectorAdapter(
        connector_id="teams-bot-connector",
        project_id=mesh_config.project.project_id,
        connector_config=mesh_config.project.connectors["teams"],
        outbox=outbox,
        journal=journal,
        secrets=FakeSecrets(),  # type: ignore[arg-type]
    )
    connector._bot_token = lambda app_id, app_secret: "bot-token"
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"id":"dm-reply-id"}'

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setattr("agentic_mesh.connectors.request.urlopen", fake_urlopen)

    message = ConnectorMessage.create(
        channel="all-agents",
        message_type="sponsor_directive.acknowledged",
        payload={
            "title": "Product question",
            "work_item_id": "work-product-dm",
            "role_id": "product-manager",
            "role_count": 1,
            "target_roles": ["product-manager"],
            "source_channel": "dm",
            "teams_service_url": "https://smba.trafficmanager.net/uk/",
            "teams_conversation_id": "personal-conversation-1",
            "teams_reply_to_activity_id": "activity-product-manager-dm",
        },
        source="test",
    )

    connector._post_message(message, "product-manager")

    assert captured["url"].endswith(
        "/v3/conversations/personal-conversation-1/"
        "activities/activity-product-manager-dm"
    )
    assert captured["body"]["from"] == {
        "id": "product-app-id",
        "name": "AM-Product Manager",
        "role": "bot",
    }
    assert captured["body"]["channelData"] == {
        "tenant": {"id": mesh_config.project.connectors["teams"].tenant_id},
        "agenticMesh": {"senderRole": "product-manager", "sourceScope": "dm"},
    }


def test_bot_connector_renders_sponsor_directive_status() -> None:
    message = ConnectorMessage.create(
        channel="release",
        message_type="sponsor_directive.completed",
        payload={
            "title": "Adopt this project",
            "work_item_id": "work-adoption",
            "git_branch": "codex/work-adoption-adopt-this-project",
            "role_id": "release-manager",
            "role_instance_id": "agentic-mesh-dev.release-manager.1",
            "status": "blocked",
            "status_message": "Codex CLI failed before returning a valid agent result.",
            "artifact_paths": ["documents/analysis/release-manager.md"],
        },
        source="test",
    )

    rendered = BotFrameworkTeamsConnectorAdapter._render_text(message)

    assert "release-manager: blocked direct instruction" in rendered
    assert "work-adoption" in rendered
    assert "Codex CLI failed" in rendered
    assert "documents/analysis/release-manager.md" in rendered
    assert "codex/work-adoption-adopt-this-project" in rendered


def test_bot_connector_renders_sponsor_directive_publish_ready() -> None:
    message = ConnectorMessage.create(
        channel="all-agents",
        message_type="sponsor_directive.publish_ready",
        payload={
            "title": "Adopt this project",
            "work_item_id": "work-adoption",
            "git_branch": "codex/work-adoption-adopt-this-project",
            "artifact_paths": ["documents/analysis/release-manager.md"],
        },
        source="test",
    )

    rendered = BotFrameworkTeamsConnectorAdapter._render_text(message)

    assert "ready to publish" in rendered
    assert "work-adoption" in rendered
    assert "codex/work-adoption-adopt-this-project" in rendered


def test_bot_connector_renders_blocked_directive_terminal_summary() -> None:
    message = ConnectorMessage.create(
        channel="all-agents",
        message_type="sponsor_directive.publish_ready",
        payload={
            "title": "Create Mermaid CLI slice",
            "work_item_id": "work-mermaid",
            "git_branch": "codex/work-mermaid",
            "publication": {"status": "terminal_with_blockers"},
            "terminal_status": "terminal_with_blockers",
            "blocked_roles": ["delivery-manager"],
            "artifact_paths": [],
        },
        source="test",
    )

    rendered = BotFrameworkTeamsConnectorAdapter._render_text(message)

    assert "needs sponsor review" in rendered
    assert "ready to publish" not in rendered
    assert "Blocked roles: delivery-manager" in rendered
    assert "Artifacts: none" in rendered


def test_teams_ingress_human_response_joins_trace_context(tmp_path: Path) -> None:
    sink = telemetry.TelemetryTestSink()
    telemetry.set_test_sink(sink)
    try:
        mesh_config = load_mesh_config(Path.cwd())
        state_root = tmp_path / "state"
        journal = EventJournal(state_root, mesh_config.project.project_id)
        message_store = FileMessageStore(state_root, mesh_config.project.project_id, journal)
        _seed_release_approval_context(
            state_root,
            project_id=mesh_config.project.project_id,
            correlation_id="corr-1234567890abcdef1234567890abcdef",
        )
        ingress = TeamsBotIngress(
            connector_id="teams-bot-listener",
            project_id=mesh_config.project.project_id,
            state_root=state_root,
            message_store=message_store,
            journal=journal,
        )

        ingress.receive_activity(
            {
                "type": "message",
                "id": "activity/trace",
                "serviceUrl": "https://smba.trafficmanager.net/uk/tenant/",
                "from": {"id": "user-1", "name": "Nich"},
                "conversation": {"id": "conversation-1"},
                "replyToId": "activity-original",
                "value": {
                    "action": "human_response.submit",
                    "role_id": "release-manager",
                    "work_item_id": "slice-release",
                    "work_item_type": "slice",
                    "lifecycle_state": "release_review",
                    "gate_id": "release_decision_response",
                    "response_request_id": "human-response-1",
                    "response_value": "approved",
                    "correlation_id": "corr-1234567890abcdef1234567890abcdef",
                    "trace_context": {
                        "trace_id": "1234567890abcdef1234567890abcdef"
                    },
                },
            }
        )
    finally:
        telemetry.set_test_sink(None)

    span_names = [span.name for span in sink.spans]
    assert "teams.receive" in span_names
    assert "human_response.wait" in span_names
    assert "work.enqueue" in span_names
    assert all(
        span.trace_context["trace_id"] == "1234567890abcdef1234567890abcdef"
        for span in sink.spans
        if span.name in {"teams.receive", "human_response.wait", "work.enqueue"}
    )


def test_connector_status_link_does_not_fall_back_to_auth_admin_url(monkeypatch) -> None:
    monkeypatch.delenv("AGENTIC_MESH_STATUS_BASE_URL", raising=False)
    monkeypatch.setenv("AGENTIC_MESH_AUTH_ADMIN_URL", "http://127.0.0.1:8100/auth/status")
    assert _work_item_status_url("work-activation") == ""

    monkeypatch.setenv("AGENTIC_MESH_STATUS_BASE_URL", "http://127.0.0.1:8100")
    assert _work_item_status_url("work-activation") == ""

    monkeypatch.setenv("AGENTIC_MESH_STATUS_BASE_URL", "http://controller.local")
    assert _work_item_status_url("work-activation") == ""

    monkeypatch.setenv("AGENTIC_MESH_STATUS_BASE_URL", "https://status.example.com")
    assert (
        _work_item_status_url("work-activation")
        == "https://status.example.com/work-items/work-activation"
    )
