import json
from pathlib import Path

from agentic_mesh.control_plane_actions import ACTION_AUTHENTICATE_PROFILE
from agentic_mesh.control_plane_actions import ACTION_BIND_COMMENT_THREAD
from agentic_mesh.control_plane_actions import ACTION_CAPTURE_QUEUE_ITEM
from agentic_mesh.control_plane_actions import ACTION_MARK_QUEUE_READY
from agentic_mesh.control_plane_actions import ACTION_PROMOTE_QUEUE_ITEM
from agentic_mesh.control_plane_actions import ACTION_READ_AUDIT
from agentic_mesh.control_plane_actions import ACTION_READ_PROJECT_STATUS
from agentic_mesh.control_plane_actions import ACTION_READ_QUEUE_ITEM
from agentic_mesh.control_plane_actions import ACTION_READ_RAW_REFERENCE
from agentic_mesh.control_plane_actions import ACTION_READ_WORK_ITEM
from agentic_mesh.control_plane_actions import ACTION_RELOAD_CONFIG
from agentic_mesh.control_plane_actions import ACTION_RETRY_ROUTE_OR_NOTIFICATION
from agentic_mesh.control_plane_actions import ACTION_SUBMIT_APPROVAL_RESPONSE
from agentic_mesh.control_plane_actions import ACTION_TRANSITION_QUEUE_ITEM
from agentic_mesh.control_plane_actions import ACTION_UNBLOCK_ROUTE_OR_PROBLEM
from agentic_mesh.control_plane_actions import CONTROL_PLANE_ACTION_REGISTRY
from agentic_mesh.control_plane_actions import RECOVERY_ALERT_ACKNOWLEDGE_ACTION
from agentic_mesh.control_plane_actions import RECOVERY_ALERT_SILENCE_ACTION
from agentic_mesh.control_plane_actions import RECOVERY_OBSERVABILITY_READ_ACTION
from agentic_mesh.control_plane_actions import FileControlPlaneActionStore
from agentic_mesh.control_plane_actions import ControlPlaneActionPolicy
from agentic_mesh.control_plane_actions import ControlPlaneActionService
from agentic_mesh.control_plane_actions import ControlPlaneActionTarget
from agentic_mesh.control_plane_actions import ControlPlaneActor
from agentic_mesh.control_plane_actions import control_plane_receipt_output
from agentic_mesh.control_plane_actions import control_plane_request
from agentic_mesh.control_plane_actions import registry_snapshot
from agentic_mesh.journal import EventJournal
from agentic_mesh.storage import FileMessageStore
from agentic_mesh.work_queue import FileWorkQueueStore
from agentic_mesh.work_queue import SourceAnchor
from agentic_mesh.work_item_recovery import RECOVERY_MUTATION_ACTIONS
from agentic_mesh.work_item_recovery import RECOVERY_READ_ACTION


def _anchor() -> SourceAnchor:
    return SourceAnchor(
        connector_type="teams",
        connector_id="teams-bot-listener",
        source_scope="approvals",
        source_message_id="raw/activity/123",
        actor="raw-teams-user",
        received_at="2026-06-05T10:00:00+00:00",
        display_label="Sponsor",
        external_url="https://service.example/raw",
        extensions={"tenant_id": "raw-tenant", "channel_id": "raw-channel"},
    )


def _service(
    tmp_path: Path,
    *,
    allow_cli_promotion: bool = False,
) -> tuple[ControlPlaneActionService, FileWorkQueueStore, FileMessageStore]:
    journal = EventJournal(tmp_path / "state", "agentic-mesh-dev")
    work_queue = FileWorkQueueStore(tmp_path / "state", "agentic-mesh-dev", journal)
    message_store = FileMessageStore(tmp_path / "state", "agentic-mesh-dev", journal)
    service = ControlPlaneActionService(
        project_id="agentic-mesh-dev",
        work_queue=work_queue,
        store=FileControlPlaneActionStore(tmp_path / "state", "agentic-mesh-dev"),
        journal=journal,
        policy=ControlPlaneActionPolicy(allow_cli_promotion=allow_cli_promotion),
        message_store=message_store,
    )
    return service, work_queue, message_store


def test_control_plane_registry_is_table_driven_and_mcp_is_metadata_only() -> None:
    registry = CONTROL_PLANE_ACTION_REGISTRY
    expected_actions = {
        ACTION_AUTHENTICATE_PROFILE,
        ACTION_READ_PROJECT_STATUS,
        ACTION_CAPTURE_QUEUE_ITEM,
        ACTION_READ_QUEUE_ITEM,
        ACTION_TRANSITION_QUEUE_ITEM,
        ACTION_MARK_QUEUE_READY,
        ACTION_PROMOTE_QUEUE_ITEM,
        ACTION_READ_WORK_ITEM,
        ACTION_SUBMIT_APPROVAL_RESPONSE,
        ACTION_BIND_COMMENT_THREAD,
        ACTION_RETRY_ROUTE_OR_NOTIFICATION,
        ACTION_UNBLOCK_ROUTE_OR_PROBLEM,
        ACTION_RELOAD_CONFIG,
        ACTION_READ_AUDIT,
        ACTION_READ_RAW_REFERENCE,
        RECOVERY_READ_ACTION,
        RECOVERY_OBSERVABILITY_READ_ACTION,
        RECOVERY_ALERT_ACKNOWLEDGE_ACTION,
        RECOVERY_ALERT_SILENCE_ACTION,
        *RECOVERY_MUTATION_ACTIONS,
    }

    assert set(registry) == expected_actions
    assert registry[ACTION_AUTHENTICATE_PROFILE].target_type == "profile"
    assert registry[ACTION_AUTHENTICATE_PROFILE].sensitive is True
    assert registry[ACTION_AUTHENTICATE_PROFILE].mutation is True
    assert registry[ACTION_AUTHENTICATE_PROFILE].mcp_ready is False
    assert registry[ACTION_AUTHENTICATE_PROFILE].enabled_by_default is False
    assert registry[ACTION_CAPTURE_QUEUE_ITEM].target_type == "queue"
    assert registry[ACTION_CAPTURE_QUEUE_ITEM].idempotency_required is True
    assert registry[ACTION_PROMOTE_QUEUE_ITEM].enabled_by_default is False
    assert registry[ACTION_PROMOTE_QUEUE_ITEM].mcp_ready is True
    assert registry[RECOVERY_OBSERVABILITY_READ_ACTION].mutation is False
    assert registry[RECOVERY_ALERT_ACKNOWLEDGE_ACTION].mutation is True
    assert registry[RECOVERY_ALERT_ACKNOWLEDGE_ACTION].enabled_by_default is False
    assert registry[RECOVERY_ALERT_SILENCE_ACTION].mutation is True
    assert registry[RECOVERY_ALERT_SILENCE_ACTION].enabled_by_default is False
    assert "mcp_server" not in json.dumps(registry_snapshot())


def test_control_plane_capture_uses_control_plane_schema_and_state_namespace(
    tmp_path: Path,
) -> None:
    service, work_queue, _ = _service(tmp_path)
    request = control_plane_request(
        action_type=ACTION_CAPTURE_QUEUE_ITEM,
        source_type="cli",
        project_id="agentic-mesh-dev",
        profile_ref="operator-default",
        source_anchor=_anchor(),
        actor=ControlPlaneActor(
            actor_type="operator",
            display_label="Ops User",
            subject_ref="raw-provider-subject",
            auth_context_ref="profile-session",
            identity_provider="local_dev",
        ),
        payload={
            "title": "Official control-plane action",
            "summary": "Captured through the approved boundary.",
            "owner_role": "product-manager",
            "recommended_work_item_type": "slice",
        },
        idempotency_key="control-plane-capture",
    )

    receipt = service.execute(request)
    output = control_plane_receipt_output(receipt)
    rendered = json.dumps(output)

    assert output["schema_version"] == "control-plane-action-receipt-v0"
    assert receipt.target.queue_item_id is not None
    assert len(work_queue.list_items()) == 1
    assert (tmp_path / "state/projects/agentic-mesh-dev/control_plane").exists()
    assert not (tmp_path / "state/projects/agentic-mesh-dev/external_actions").exists()
    assert output["profile_summary"]["profile_ref"] == "operator-default"
    assert "raw-provider-subject" not in rendered
    assert "raw/activity/123" not in rendered
    assert "raw-teams-user" not in rendered
    assert "raw-tenant" not in rendered
    assert "service.example" not in rendered


def test_unauthenticated_mutation_is_denied_without_queue_mutation(
    tmp_path: Path,
) -> None:
    service, work_queue, message_store = _service(tmp_path, allow_cli_promotion=True)
    request = control_plane_request(
        action_type=ACTION_PROMOTE_QUEUE_ITEM,
        source_type="cli",
        project_id="agentic-mesh-dev",
        source_anchor=_anchor(),
        actor=ControlPlaneActor(actor_type="unauthenticated", display_label="anonymous"),
        target=ControlPlaneActionTarget(queue_item_id="queue-missing"),
        payload={"target_role": "business-analyst", "lifecycle_state": "business_analysis"},
        reason="attempted unauthenticated promotion",
    )

    receipt = service.execute(request)
    output = control_plane_receipt_output(receipt)

    assert output["outcome"] == "action_denied"
    assert output["permission_decision"] == "denied"
    assert output["decision_reason"] == "unauthenticated"
    assert output["safe_reason"] == "unauthenticated"
    assert work_queue.list_items() == []
    assert message_store.pending_count("business-analyst") == 0
