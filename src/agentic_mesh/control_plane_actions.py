from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agentic_mesh.external_actions import ACTION_AUTHENTICATE_PROFILE
from agentic_mesh.external_actions import ACTION_BIND_COMMENT_THREAD
from agentic_mesh.external_actions import ACTION_CAPTURE_QUEUE_ITEM
from agentic_mesh.external_actions import ACTION_MARK_QUEUE_READY
from agentic_mesh.external_actions import ACTION_PROMOTE_QUEUE_ITEM
from agentic_mesh.external_actions import ACTION_READ_AUDIT
from agentic_mesh.external_actions import ACTION_READ_PROJECT_STATUS
from agentic_mesh.external_actions import ACTION_READ_QUEUE_ITEM
from agentic_mesh.external_actions import ACTION_READ_RAW_REFERENCE
from agentic_mesh.external_actions import ACTION_READ_WORK_ITEM
from agentic_mesh.external_actions import ACTION_RELOAD_CONFIG
from agentic_mesh.external_actions import ACTION_RETRY_ROUTE_OR_NOTIFICATION
from agentic_mesh.external_actions import ACTION_SUBMIT_APPROVAL_RESPONSE
from agentic_mesh.external_actions import ACTION_TRANSITION_QUEUE_ITEM
from agentic_mesh.external_actions import ACTION_UNBLOCK_ROUTE_OR_PROBLEM
from agentic_mesh.external_actions import CONTROL_PLANE_ACTION_SCHEMA_VERSION
from agentic_mesh.external_actions import CONTROL_PLANE_RECEIPT_SCHEMA_VERSION
from agentic_mesh.external_actions import ExternalActionPolicy
from agentic_mesh.external_actions import ExternalActionReceipt
from agentic_mesh.external_actions import ExternalActionRequest
from agentic_mesh.external_actions import ExternalActionService
from agentic_mesh.external_actions import ExternalActionTarget
from agentic_mesh.external_actions import ExternalActor
from agentic_mesh.external_actions import FileExternalActionStore
from agentic_mesh.external_actions import safe_payload
from agentic_mesh.external_actions import safe_token
from agentic_mesh.work_item_recovery import RECOVERY_READ_ACTION
from agentic_mesh.work_item_recovery import RECOVERY_MUTATION_ACTIONS


RECOVERY_OBSERVABILITY_READ_ACTION = "read_recovery_observability"
RECOVERY_ALERT_ACKNOWLEDGE_ACTION = "acknowledge_recovery_alert"
RECOVERY_ALERT_SILENCE_ACTION = "silence_recovery_alert"


@dataclass(frozen=True)
class ControlPlaneActionRegistryEntry:
    action_type: str
    target_type: str
    permission_key: str
    sensitive: bool
    mutation: bool
    reason_required: bool = False
    idempotency_required: bool = False
    enabled_by_default: bool = True
    mcp_ready: bool = False

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "action_type": safe_token(self.action_type),
            "target_type": safe_token(self.target_type),
            "permission_key": safe_token(self.permission_key),
            "sensitive": self.sensitive,
            "mutation": self.mutation,
            "reason_required": self.reason_required,
            "idempotency_required": self.idempotency_required,
            "enabled_by_default": self.enabled_by_default,
            "mcp_ready": self.mcp_ready,
        }


CONTROL_PLANE_ACTION_REGISTRY: dict[str, ControlPlaneActionRegistryEntry] = {
    ACTION_AUTHENTICATE_PROFILE: ControlPlaneActionRegistryEntry(
        ACTION_AUTHENTICATE_PROFILE,
        "profile",
        "auth.authenticate_profile",
        sensitive=True,
        mutation=True,
        idempotency_required=True,
        enabled_by_default=False,
    ),
    ACTION_READ_PROJECT_STATUS: ControlPlaneActionRegistryEntry(
        ACTION_READ_PROJECT_STATUS,
        "project",
        "status.read",
        sensitive=False,
        mutation=False,
        mcp_ready=True,
    ),
    ACTION_CAPTURE_QUEUE_ITEM: ControlPlaneActionRegistryEntry(
        ACTION_CAPTURE_QUEUE_ITEM,
        "queue",
        "queue.capture",
        sensitive=False,
        mutation=True,
        idempotency_required=True,
        mcp_ready=True,
    ),
    ACTION_READ_QUEUE_ITEM: ControlPlaneActionRegistryEntry(
        ACTION_READ_QUEUE_ITEM,
        "queue_item",
        "queue.read",
        sensitive=True,
        mutation=False,
        mcp_ready=True,
    ),
    ACTION_TRANSITION_QUEUE_ITEM: ControlPlaneActionRegistryEntry(
        ACTION_TRANSITION_QUEUE_ITEM,
        "queue_item",
        "queue.transition",
        sensitive=True,
        mutation=True,
        reason_required=True,
        idempotency_required=True,
        mcp_ready=True,
    ),
    ACTION_MARK_QUEUE_READY: ControlPlaneActionRegistryEntry(
        ACTION_MARK_QUEUE_READY,
        "queue_item",
        "queue.mark_ready",
        sensitive=True,
        mutation=True,
        idempotency_required=True,
        mcp_ready=True,
    ),
    ACTION_PROMOTE_QUEUE_ITEM: ControlPlaneActionRegistryEntry(
        ACTION_PROMOTE_QUEUE_ITEM,
        "queue_item",
        "queue.promote",
        sensitive=True,
        mutation=True,
        reason_required=True,
        idempotency_required=True,
        enabled_by_default=False,
        mcp_ready=True,
    ),
    ACTION_READ_WORK_ITEM: ControlPlaneActionRegistryEntry(
        ACTION_READ_WORK_ITEM,
        "work_item",
        "work.read",
        sensitive=True,
        mutation=False,
        mcp_ready=True,
    ),
    ACTION_SUBMIT_APPROVAL_RESPONSE: ControlPlaneActionRegistryEntry(
        ACTION_SUBMIT_APPROVAL_RESPONSE,
        "approval_gate",
        "approval.respond",
        sensitive=True,
        mutation=True,
        idempotency_required=True,
        mcp_ready=True,
    ),
    ACTION_BIND_COMMENT_THREAD: ControlPlaneActionRegistryEntry(
        ACTION_BIND_COMMENT_THREAD,
        "source_anchor",
        "source.bind_comment_thread",
        sensitive=True,
        mutation=True,
        mcp_ready=True,
    ),
    ACTION_RETRY_ROUTE_OR_NOTIFICATION: ControlPlaneActionRegistryEntry(
        ACTION_RETRY_ROUTE_OR_NOTIFICATION,
        "route_problem",
        "route.retry",
        sensitive=True,
        mutation=True,
        reason_required=True,
        mcp_ready=True,
    ),
    ACTION_UNBLOCK_ROUTE_OR_PROBLEM: ControlPlaneActionRegistryEntry(
        ACTION_UNBLOCK_ROUTE_OR_PROBLEM,
        "route_problem",
        "route.unblock",
        sensitive=True,
        mutation=True,
        reason_required=True,
        enabled_by_default=False,
        mcp_ready=True,
    ),
    ACTION_RELOAD_CONFIG: ControlPlaneActionRegistryEntry(
        ACTION_RELOAD_CONFIG,
        "project",
        "config.reload",
        sensitive=True,
        mutation=True,
        reason_required=True,
        enabled_by_default=False,
    ),
    ACTION_READ_AUDIT: ControlPlaneActionRegistryEntry(
        ACTION_READ_AUDIT,
        "audit",
        "audit.read",
        sensitive=True,
        mutation=False,
        mcp_ready=True,
    ),
    ACTION_READ_RAW_REFERENCE: ControlPlaneActionRegistryEntry(
        ACTION_READ_RAW_REFERENCE,
        "support_ref",
        "support.read_raw_reference",
        sensitive=True,
        mutation=False,
        reason_required=True,
        enabled_by_default=False,
    ),
    RECOVERY_READ_ACTION: ControlPlaneActionRegistryEntry(
        RECOVERY_READ_ACTION,
        "work_item",
        "recovery.read",
        sensitive=True,
        mutation=False,
        mcp_ready=False,
    ),
    RECOVERY_OBSERVABILITY_READ_ACTION: ControlPlaneActionRegistryEntry(
        RECOVERY_OBSERVABILITY_READ_ACTION,
        "work_item",
        "recovery_observability.read",
        sensitive=False,
        mutation=False,
        mcp_ready=False,
    ),
    RECOVERY_ALERT_ACKNOWLEDGE_ACTION: ControlPlaneActionRegistryEntry(
        RECOVERY_ALERT_ACKNOWLEDGE_ACTION,
        "recovery_alert",
        "recovery_alert.acknowledge",
        sensitive=True,
        mutation=True,
        reason_required=True,
        idempotency_required=False,
        enabled_by_default=False,
        mcp_ready=False,
    ),
    RECOVERY_ALERT_SILENCE_ACTION: ControlPlaneActionRegistryEntry(
        RECOVERY_ALERT_SILENCE_ACTION,
        "recovery_alert",
        "recovery_alert.silence",
        sensitive=True,
        mutation=True,
        reason_required=True,
        idempotency_required=False,
        enabled_by_default=False,
        mcp_ready=False,
    ),
}

for _recovery_action in sorted(RECOVERY_MUTATION_ACTIONS):
    CONTROL_PLANE_ACTION_REGISTRY[_recovery_action] = ControlPlaneActionRegistryEntry(
        _recovery_action,
        "work_item",
        f"recovery.{_recovery_action.removesuffix('_work_item_recovery')}",
        sensitive=True,
        mutation=True,
        reason_required=True,
        idempotency_required=True,
        enabled_by_default=False,
        mcp_ready=False,
    )


class FileControlPlaneActionStore(FileExternalActionStore):
    def __init__(self, state_root, project_id: str) -> None:
        super().__init__(state_root, project_id, namespace="control_plane")


class ControlPlaneActionPolicy(ExternalActionPolicy):
    pass


class ControlPlaneActionService(ExternalActionService):
    pass


ControlPlaneActor = ExternalActor
ControlPlaneActionTarget = ExternalActionTarget


def control_plane_request(**kwargs: Any) -> ExternalActionRequest:
    return ExternalActionRequest(
        schema_version=CONTROL_PLANE_ACTION_SCHEMA_VERSION,
        **kwargs,
    )


def control_plane_receipt_output(receipt: ExternalActionReceipt) -> dict[str, Any]:
    payload = receipt.to_safe_dict()
    payload["schema_version"] = CONTROL_PLANE_RECEIPT_SCHEMA_VERSION
    return safe_payload(payload)


def registry_snapshot() -> list[dict[str, Any]]:
    return [
        entry.to_safe_dict()
        for _, entry in sorted(CONTROL_PLANE_ACTION_REGISTRY.items())
    ]
