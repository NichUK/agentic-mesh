from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentic_mesh.config import load_mesh_config
from agentic_mesh.connectors import TeamsBotIngress
from agentic_mesh.gateway import GatewayError
from agentic_mesh.gateway import GatewayService
from agentic_mesh.gateway import GatewayStore
from agentic_mesh.gateway import OUTCOME_CLARIFICATION_NEEDED
from agentic_mesh.gateway import OUTCOME_CREATE_OR_UPDATE_QUEUE_ITEM
from agentic_mesh.gateway import OUTCOME_ROLE_DIRECTED_HINT
from agentic_mesh.gateway import OUTCOME_STATUS_ANSWER
from agentic_mesh.gateway import assert_no_forbidden_fields
from agentic_mesh.gateway import event_from_message
from agentic_mesh.journal import EventJournal
from agentic_mesh.models import GatewayBotConfig
from agentic_mesh.models import GatewayConfig
from agentic_mesh.models import GatewayTeamsConfig
from agentic_mesh.storage import FileConnectorOutbox
from agentic_mesh.storage import FileMessageStore
from agentic_mesh.work_queue import FileWorkQueueStore
from agentic_mesh.work_queue import SourceAnchor


class StatusReader:
    def answer_status(self, ref: str) -> dict[str, str] | None:
        if ref == "work-known":
            return {"work_item_id": ref, "status": "running", "owner_role": "engineering"}
        return None


def _journal(tmp_path: Path) -> EventJournal:
    return EventJournal(tmp_path, "agentic-mesh-dev")


def _source_anchor() -> SourceAnchor:
    return SourceAnchor(
        connector_type="teams",
        connector_id="teams",
        source_scope="all-agents",
        source_message_id="message-1",
        actor="Sponsor",
        received_at="2026-06-06T00:00:00+00:00",
        display_label="Sponsor in all-agents",
    )


def _gateway_config() -> GatewayConfig:
    return GatewayConfig(
        gateway_id="agentic-mesh",
        default_owner_role="delivery-manager",
        teams=GatewayTeamsConfig(
            connector="teams",
            bot=GatewayBotConfig(
                display_name="AM-Agentic Mesh",
                bot_id_ref="teams-bot-agentic-mesh-app-id",
                secret_ref="teams-bot-agentic-mesh-secret",
            ),
            intake_channels=["all-agents"],
        ),
    )


def test_gateway_event_safe_serialization_redacts_forbidden_values() -> None:
    event = event_from_message(
        project_id="agentic-mesh-dev",
        gateway_id="agentic-mesh",
        connector_type="teams",
        connector_id="teams",
        source_kind="channel",
        source_anchor=_source_anchor(),
        actor_label="Sponsor",
        actor_source_id="raw-user-id",
        text="Please implement this token=secret-value at /mesh/private/path",
        idempotency_key="teams:all-agents:message-1",
        role_hints=["engineering"],
    )

    safe = event.to_safe_dict()

    assert safe["actor"]["actor_ref"].startswith("actor-")
    assert "raw-user-id" not in json.dumps(safe)
    assert "secret-value" not in json.dumps(safe)
    assert "/mesh/private/path" not in json.dumps(safe)
    assert_no_forbidden_fields(safe)
    with pytest.raises(GatewayError):
        assert_no_forbidden_fields({"tenant_id": "raw"})


def test_gateway_service_keeps_teams_delivery_request_conversational(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    work_queue = FileWorkQueueStore(tmp_path, "agentic-mesh-dev", journal)
    service = GatewayService(
        project_id="agentic-mesh-dev",
        gateway_config=_gateway_config(),
        store=GatewayStore(tmp_path, "agentic-mesh-dev"),
        journal=journal,
        work_queue=work_queue,
        role_ids={"delivery-manager", "engineering"},
    )
    event = event_from_message(
        project_id="agentic-mesh-dev",
        gateway_id="agentic-mesh",
        connector_type="teams",
        connector_id="teams",
        source_kind="channel",
        source_anchor=_source_anchor(),
        actor_label="Sponsor",
        actor_source_id="actor-1",
        text="Please implement this gateway change and just do it.",
        idempotency_key="teams:all-agents:message-2",
        role_hints=["engineering"],
    )

    result = service.handle_event(event)
    duplicate = service.handle_event(event)

    assert result.outcome == OUTCOME_ROLE_DIRECTED_HINT
    assert result.queue_item_id is None
    assert result.owner_role == "engineering"
    assert duplicate.gateway_result_id == result.gateway_result_id
    assert work_queue.list_items() == []
    assert not (tmp_path / "projects" / "agentic-mesh-dev" / "queues").exists()


def test_gateway_service_creates_queue_from_work_item_native_source(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    work_queue = FileWorkQueueStore(tmp_path, "agentic-mesh-dev", journal)
    service = GatewayService(
        project_id="agentic-mesh-dev",
        gateway_config=_gateway_config(),
        store=GatewayStore(tmp_path, "agentic-mesh-dev"),
        journal=journal,
        work_queue=work_queue,
        role_ids={"delivery-manager", "engineering"},
    )
    event = event_from_message(
        project_id="agentic-mesh-dev",
        gateway_id="agentic-mesh",
        connector_type="github",
        connector_id="github-issues",
        source_kind="issue",
        source_anchor=SourceAnchor(
            connector_type="github",
            connector_id="github-issues",
            source_scope="NichUK/agentic-mesh",
            source_message_id="42",
            actor="Sponsor",
            received_at="2026-06-06T00:00:00+00:00",
            display_label="GitHub issue 42",
        ),
        actor_label="Sponsor",
        actor_source_id="actor-1",
        text="Please implement the gateway status command.",
        idempotency_key="github:issue:42",
        role_hints=["engineering"],
    )

    result = service.handle_event(event)

    assert result.outcome == OUTCOME_CREATE_OR_UPDATE_QUEUE_ITEM
    assert result.queue_item_id is not None
    assert result.owner_role == "delivery-manager"
    assert len(work_queue.list_items()) == 1


def test_gateway_status_answer_uses_safe_reader_without_queue_creation(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    work_queue = FileWorkQueueStore(tmp_path, "agentic-mesh-dev", journal)
    service = GatewayService(
        project_id="agentic-mesh-dev",
        gateway_config=_gateway_config(),
        store=GatewayStore(tmp_path, "agentic-mesh-dev"),
        journal=journal,
        work_queue=work_queue,
        status_reader=StatusReader(),
    )
    event = event_from_message(
        project_id="agentic-mesh-dev",
        gateway_id="agentic-mesh",
        connector_type="teams",
        connector_id="teams",
        source_kind="dm",
        source_anchor=_source_anchor(),
        actor_label="Sponsor",
        actor_source_id="actor-1",
        text="status work-known",
        idempotency_key="teams:dm:message-3",
    )

    result = service.handle_event(event)

    assert result.outcome == OUTCOME_STATUS_ANSWER
    assert result.queue_item_id is None
    assert work_queue.list_items() == []


def test_gateway_status_unknown_ref_clarifies_without_queue(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    work_queue = FileWorkQueueStore(tmp_path, "agentic-mesh-dev", journal)
    service = GatewayService(
        project_id="agentic-mesh-dev",
        gateway_config=_gateway_config(),
        store=GatewayStore(tmp_path, "agentic-mesh-dev"),
        journal=journal,
        work_queue=work_queue,
        status_reader=StatusReader(),
    )
    event = event_from_message(
        project_id="agentic-mesh-dev",
        gateway_id="agentic-mesh",
        connector_type="teams",
        connector_id="teams",
        source_kind="dm",
        source_anchor=_source_anchor(),
        actor_label="Sponsor",
        actor_source_id="actor-1",
        text="status work-missing",
        idempotency_key="teams:dm:message-4",
    )

    result = service.handle_event(event)

    assert result.outcome == OUTCOME_CLARIFICATION_NEEDED
    assert work_queue.list_items() == []


def test_teams_gateway_configured_channel_keeps_message_conversational(
    tmp_path: Path,
) -> None:
    mesh_config = load_mesh_config(Path.cwd())
    journal = _journal(tmp_path)
    outbox = FileConnectorOutbox(tmp_path, "agentic-mesh-dev", journal)
    ingress = TeamsBotIngress(
        connector_id="teams",
        project_id="agentic-mesh-dev",
        state_root=tmp_path,
        message_store=FileMessageStore(tmp_path, "agentic-mesh-dev", journal),
        journal=journal,
        connector_config=mesh_config.project.connectors["teams"],
        project_config=mesh_config.project,
        connector_outbox=outbox,
    )

    result = ingress.receive_activity(
        {
            "type": "message",
            "id": "activity-1",
            "text": "<at>agentic-mesh</at> please implement a status command",
            "from": {"id": "user-1", "name": "Sponsor"},
            "conversation": {
                "id": mesh_config.project.connectors["teams"].channels["all-agents"].channel_id
            },
            "channelData": {
                "team": {"id": "team-raw"},
                "channel": {
                    "id": mesh_config.project.connectors["teams"]
                    .channels["all-agents"]
                    .channel_id,
                    "name": "all-agents",
                },
            },
            "entities": [{"type": "mention", "text": "agentic-mesh"}],
        }
    )

    assert result["status"] == "accepted"
    work_items = FileWorkQueueStore(tmp_path, "agentic-mesh-dev", journal).list_items()
    assert work_items == []
    assert all(
        ingress.message_store.pending_count(role_id) == 0
        for role_id in mesh_config.project.roles
    )
    assert (tmp_path / "projects" / "agentic-mesh-dev" / "gateway").exists()


def test_teams_non_gateway_role_channel_ignored(tmp_path: Path) -> None:
    mesh_config = load_mesh_config(Path.cwd())
    journal = _journal(tmp_path)
    ingress = TeamsBotIngress(
        connector_id="teams",
        project_id="agentic-mesh-dev",
        state_root=tmp_path,
        message_store=FileMessageStore(tmp_path, "agentic-mesh-dev", journal),
        journal=journal,
        connector_config=mesh_config.project.connectors["teams"],
        project_config=mesh_config.project,
    )

    ingress.receive_activity(
        {
            "type": "message",
            "id": "activity-2",
            "text": "Please do something",
            "from": {"id": "user-1", "name": "Sponsor"},
            "conversation": {
                "id": mesh_config.project.connectors["teams"].channels["engineering"].channel_id
            },
            "channelData": {
                "team": {"id": "team-raw"},
                "channel": {
                    "id": mesh_config.project.connectors["teams"]
                    .channels["engineering"]
                    .channel_id,
                    "name": "engineering",
                },
            },
        }
    )

    items = FileWorkQueueStore(tmp_path, "agentic-mesh-dev", journal).list_items()
    assert items
    assert all("gateway_id" not in item.metadata for item in items)
    events = [event["event_type"] for event in journal.read_all()]
    assert "teams_gateway_intake_processed" not in events
