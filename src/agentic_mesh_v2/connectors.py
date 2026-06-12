from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from agentic_mesh_v2.db import V2Database
from agentic_mesh_v2.safe_outputs import SafeOutputCall
from agentic_mesh_v2.safe_outputs import SafeOutputService
from agentic_mesh_v2.safe_outputs import ToolPolicy


VALID_RETENTION_KEYS = {
    "private_dm_days",
    "project_channel_days",
    "compacted_summary_days",
    "delivery_record_days",
    "idempotency_receipt_days",
}


@dataclass(frozen=True)
class ConnectorConfig:
    connector_id: str
    project_id: str
    connector_type: str
    display_name: str
    project_team_ref: str
    default_project_channel_ref: str
    external_base_url: str
    role_identities: dict[str, str]
    human_authorities: dict[str, list[str]]
    retention: dict[str, int]
    team_wide_trigger: str

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ConnectorConfig:
        required = {
            "connector_id",
            "project_id",
            "connector_type",
            "display_name",
            "project_team_ref",
            "default_project_channel_ref",
            "external_base_url",
            "role_identities",
            "human_authorities",
            "retention",
            "team_wide_trigger",
        }
        missing = sorted(required - set(raw))
        if missing:
            raise ValueError(f"connector config missing required keys: {', '.join(missing)}")
        if raw["connector_type"] != "teams":
            raise ValueError("story 1 local adapter supports connector_type `teams`")
        role_identities = _string_map(raw["role_identities"], "role_identities")
        if not role_identities:
            raise ValueError("connector config must define at least one role identity")
        retention = _positive_int_map(raw["retention"], "retention")
        unknown_retention = sorted(set(retention) - VALID_RETENTION_KEYS)
        if unknown_retention:
            raise ValueError(f"unknown retention keys: {', '.join(unknown_retention)}")
        return cls(
            connector_id=_required_string(raw, "connector_id"),
            project_id=_required_string(raw, "project_id"),
            connector_type=_required_string(raw, "connector_type"),
            display_name=_required_string(raw, "display_name"),
            project_team_ref=_required_string(raw, "project_team_ref"),
            default_project_channel_ref=_required_string(raw, "default_project_channel_ref"),
            external_base_url=_required_string(raw, "external_base_url"),
            role_identities=role_identities,
            human_authorities=_authority_map(raw["human_authorities"]),
            retention=retention,
            team_wide_trigger=_required_string(raw, "team_wide_trigger"),
        )


@dataclass(frozen=True)
class ReplayedEvent:
    receipt_id: str
    conversation_id: str
    duplicate: bool
    route_type: str
    mentioned_roles: tuple[str, ...]


class LocalTeamsTestAdapter:
    """Deterministic Teams-like adapter used by tests before real tenant wiring."""

    def __init__(self, db: V2Database, config: ConnectorConfig) -> None:
        self.db = db
        self.config = config

    def install(self) -> None:
        self.db.upsert_connector(
            connector_id=self.config.connector_id,
            project_id=self.config.project_id,
            connector_type=self.config.connector_type,
            display_name=self.config.display_name,
            status="configured",
            health={
                "project_team_ref": self.config.project_team_ref,
                "default_project_channel_ref": self.config.default_project_channel_ref,
            },
        )
        for role_id, external_ref in self.config.role_identities.items():
            self.db.upsert_connector_participant(
                participant_id=f"{self.config.connector_id}:role:{role_id}",
                connector_id=self.config.connector_id,
                participant_type="role",
                display_name=role_id,
                external_ref=external_ref,
                role_id=role_id,
            )
        for external_ref, authorities in self.config.human_authorities.items():
            self.db.upsert_connector_participant(
                participant_id=f"{self.config.connector_id}:human:{external_ref}",
                connector_id=self.config.connector_id,
                participant_type="human",
                display_name=external_ref,
                external_ref=external_ref,
                authority=authorities,
            )

    def replay_event(self, event: dict[str, Any]) -> ReplayedEvent:
        connector_id = self.config.connector_id
        event_type = _required_string(event, "event_type")
        message_id = _required_string(event, "message_id")
        external_conversation_ref = _required_string(event, "conversation_ref")
        sender_ref = _required_string(event, "sender_ref")
        source_type = _required_string(event, "source_type")
        body = str(event.get("body", ""))
        mentioned_roles = tuple(str(role) for role in event.get("mentioned_roles", ()))
        route_type = self._route_type(source_type=source_type, mentioned_roles=mentioned_roles, body=body)
        idempotency_key = self._idempotency_key(event)
        receipt_id = f"receipt-{_stable_digest(idempotency_key)}"
        receipt = self.db.record_external_event_receipt(
            receipt_id=receipt_id,
            connector_id=connector_id,
            idempotency_key=idempotency_key,
            external_event_id=message_id,
            event_type=event_type,
            payload=event,
        )
        conversation_id = f"conversation-{_stable_digest(f'{connector_id}:{external_conversation_ref}')}"
        if receipt.duplicate:
            return ReplayedEvent(receipt.receipt_id, conversation_id, True, route_type, mentioned_roles)
        self.db.upsert_conversation(
            conversation_id=conversation_id,
            connector=connector_id,
            external_ref=external_conversation_ref,
            sponsor_ref=sender_ref if "sponsor" in self.config.human_authorities.get(sender_ref, []) else None,
        )
        self.db.upsert_connector_participant(
            participant_id=f"{connector_id}:human:{sender_ref}",
            connector_id=connector_id,
            participant_type="human",
            display_name=sender_ref,
            external_ref=sender_ref,
            authority=self.config.human_authorities.get(sender_ref, []),
        )
        thread_ref = event.get("thread_ref")
        if thread_ref:
            self.db.bind_thread(
                thread_binding_id=f"thread-{_stable_digest(f'{connector_id}:{thread_ref}')}",
                connector_id=connector_id,
                conversation_id=conversation_id,
                external_thread_ref=str(thread_ref),
                binding_type="teams_thread",
                target_ref=event.get("target_ref"),
            )
        self.db.record_conversation_event(
            conversation_event_id=f"conversation-event-{_stable_digest(f'{receipt.receipt_id}:event')}",
            conversation_id=conversation_id,
            receipt_id=receipt.receipt_id,
            connector_id=connector_id,
            event_type=route_type,
            sender_participant_id=f"{connector_id}:human:{sender_ref}",
            body_preview=body[:240],
            visibility_scope="private" if source_type == "dm" else "project",
            role_id=str(mentioned_roles[0]) if len(mentioned_roles) == 1 else None,
            thread_ref=str(thread_ref) if thread_ref else None,
            payload={
                "source_type": source_type,
                "message_id": message_id,
                "mentioned_roles": list(mentioned_roles),
                "route_type": route_type,
            },
        )
        if route_type == "role_direct_message":
            role_id = self._role_for_direct_message(event)
            if role_id is not None:
                self.db.create_role_assignment(
                    assignment_id=f"assignment-{_stable_digest(f'{receipt.receipt_id}:{role_id}')}",
                    role_id=role_id,
                    conversation_id=conversation_id,
                    source_ref=receipt.receipt_id,
                    title="Direct Teams conversation",
                    summary="Human sent a direct message to a role agent.",
                    assignment_type="direct_conversation",
                    visibility_scope="private",
                    payload={
                        "connector_id": connector_id,
                        "conversation_id": conversation_id,
                        "receipt_id": receipt.receipt_id,
                        "message_id": message_id,
                    },
                )
        if route_type == "role_mention":
            for role_id in mentioned_roles:
                self.db.create_role_assignment(
                    assignment_id=f"assignment-{_stable_digest(f'{receipt.receipt_id}:{role_id}')}",
                    role_id=role_id,
                    conversation_id=conversation_id,
                    source_ref=receipt.receipt_id,
                    title="Teams role mention",
                    summary="Human mentioned a role in a project channel.",
                    assignment_type="channel_role_mention",
                    visibility_scope="project",
                    payload={
                        "connector_id": connector_id,
                        "conversation_id": conversation_id,
                        "receipt_id": receipt.receipt_id,
                        "message_id": message_id,
                        "thread_ref": thread_ref,
                    },
                )
        if route_type == "unknown_role_mention":
            self.db.create_connector_attention_item(
                attention_id=f"attention-{_stable_digest(f'{receipt.receipt_id}:unknown-role')}",
                connector_id=connector_id,
                owner="operator",
                reason_class="unknown_role_mention",
                next_action="Map the Teams mention to a configured Agentic Mesh role or correct the message.",
                retryable=True,
                source_ref=receipt.receipt_id,
            )
        return ReplayedEvent(receipt.receipt_id, conversation_id, False, route_type, mentioned_roles)

    def send_message(
        self,
        *,
        source_ref: str,
        destination_ref: str,
        destination_type: str,
        purpose: str,
        body: str,
        role_id: str | None = None,
        fail: bool = False,
    ) -> str:
        idempotency_key = f"{self.config.connector_id}:{source_ref}:{destination_ref}:{purpose}"
        delivery_id = f"delivery-{_stable_digest(idempotency_key)}"
        self.db.create_delivery_record(
            delivery_id=delivery_id,
            connector_id=self.config.connector_id,
            source_ref=source_ref,
            destination_ref=destination_ref,
            destination_type=destination_type,
            purpose=purpose,
            role_id=role_id,
            idempotency_key=idempotency_key,
            payload={"body": body},
            status="pending",
        )
        if fail:
            self.db.update_delivery_record(
                delivery_id,
                status="failed_transient",
                error_class="simulated_send_failure",
                error_detail="Local test adapter was instructed to fail delivery.",
            )
            self.db.create_connector_attention_item(
                attention_id=f"attention-{_stable_digest(f'{delivery_id}:failure')}",
                connector_id=self.config.connector_id,
                owner="operator",
                reason_class="delivery_failed_transient",
                next_action="Inspect connector delivery failure and retry when safe.",
                retryable=True,
                source_ref=delivery_id,
            )
        else:
            self.db.update_delivery_record(
                delivery_id,
                status="sent",
                external_message_id=f"local-teams-message-{_stable_digest(delivery_id)}",
            )
        return delivery_id

    def deliver_status_reply(self, *, call_id: str, role_id: str, payload: dict[str, Any]) -> str:
        message = str(payload.get("message") or "")
        _required_string(payload, "conversation_id")
        destination_ref = _required_string(payload, "destination_ref")
        destination_type = str(payload.get("destination_type") or "dm")
        return self.send_message(
            source_ref=call_id,
            destination_ref=destination_ref,
            destination_type=destination_type,
            purpose="status.reply",
            body=message,
            role_id=role_id,
        )

    def _route_type(self, *, source_type: str, mentioned_roles: tuple[str, ...], body: str) -> str:
        if source_type == "dm":
            return "role_direct_message"
        if self.config.team_wide_trigger and self.config.team_wide_trigger in body:
            return "team_wide_prompt"
        if mentioned_roles:
            unknown = [role for role in mentioned_roles if role not in self.config.role_identities]
            if unknown:
                return "unknown_role_mention"
            return "role_mention"
        return "project_channel_context"

    def _role_for_direct_message(self, event: dict[str, Any]) -> str | None:
        role_id = event.get("target_role_id")
        if isinstance(role_id, str) and role_id in self.config.role_identities:
            return role_id
        target_ref = event.get("target_ref")
        if isinstance(target_ref, str):
            for configured_role_id, external_ref in self.config.role_identities.items():
                if target_ref == external_ref:
                    return configured_role_id
        if len(self.config.role_identities) == 1:
            return next(iter(self.config.role_identities))
        return None

    def _idempotency_key(self, event: dict[str, Any]) -> str:
        return ":".join(
            [
                self.config.connector_id,
                _required_string(event, "conversation_ref"),
                _required_string(event, "message_id"),
                _required_string(event, "event_type"),
                str(event.get("version", "0")),
            ]
        )


class ConnectorSafeOutputService(SafeOutputService):
    def __init__(
        self,
        db: V2Database,
        *,
        adapter: LocalTeamsTestAdapter,
        policy: ToolPolicy | None = None,
    ) -> None:
        super().__init__(db, policy)
        self.adapter = adapter

    def record(self, *, run_id: str, call: SafeOutputCall) -> str:
        call_id = super().record(run_id=run_id, call=call)
        if call.tool_name == "status.reply" and "conversation_id" in call.payload:
            self.adapter.deliver_status_reply(
                call_id=call_id,
                role_id=call.role_id,
                payload=call.payload,
            )
        return call_id


def _required_string(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"`{key}` must be a non-empty string")
    return value


def _string_map(value: object, name: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError(f"`{name}` must be a mapping")
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip() or not isinstance(item, str) or not item.strip():
            raise ValueError(f"`{name}` must map non-empty strings to non-empty strings")
        result[key] = item
    return result


def _authority_map(value: object) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        raise ValueError("`human_authorities` must be a mapping")
    result: dict[str, list[str]] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip() or not isinstance(item, list):
            raise ValueError("`human_authorities` must map humans to authority lists")
        result[key] = [str(authority) for authority in item]
    return result


def _positive_int_map(value: object, name: str) -> dict[str, int]:
    if not isinstance(value, dict):
        raise ValueError(f"`{name}` must be a mapping")
    result: dict[str, int] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, int) or item <= 0:
            raise ValueError(f"`{name}` must map string keys to positive integers")
        result[key] = item
    return result


def _stable_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
