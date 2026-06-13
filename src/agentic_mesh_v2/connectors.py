from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentic_mesh_v2.db import V2Database
from agentic_mesh_v2.permissions import ConnectorPermissionModel
from agentic_mesh_v2.permissions import PermissionValidationFailure
from agentic_mesh_v2.permissions import reject_inline_secrets
from agentic_mesh_v2.permissions import runtime_capabilities_for_receive
from agentic_mesh_v2.permissions import runtime_capabilities_for_response
from agentic_mesh_v2.permissions import runtime_capabilities_for_send
from agentic_mesh_v2.permissions import startup_capabilities_for
from agentic_mesh_v2.observability import span
from agentic_mesh_v2.release import ReleaseService
from agentic_mesh_v2.safe_outputs import SafeOutputCall
from agentic_mesh_v2.safe_outputs import SafeOutputService
from agentic_mesh_v2.safe_outputs import ToolPolicy


VALID_RETENTION_KEYS = {
    "private_dm_days",
    "project_channel_days",
    "focus_channel_days",
    "compacted_summary_days",
    "delivery_record_days",
    "idempotency_receipt_days",
}

DELIVERY_OUTCOMES = {"sent", "failed_transient", "failed_permanent", "unknown"}
RETRYABLE_DELIVERY_STATUSES = {"failed_transient", "unknown", "retry_scheduled"}
IDENTITY_MODELS = {"separate_bot", "shared_gateway", "hybrid"}


@dataclass(frozen=True)
class RoleIdentity:
    role_id: str
    external_ref: str
    secret_ref: str | None
    display_name: str
    alias: str
    mention_handle: str
    identity_model: str
    enabled: bool


@dataclass(frozen=True)
class ChannelBinding:
    channel_ref: str
    scope_type: str
    display_name: str
    visibility: str
    work_scope: str | None
    private: bool


@dataclass(frozen=True)
class ConnectorConfig:
    connector_id: str
    project_id: str
    connector_type: str
    display_name: str
    tenant_id: str | None
    project_team_ref: str
    default_project_channel_ref: str
    external_base_url: str
    role_identities: dict[str, RoleIdentity]
    channel_bindings: dict[str, ChannelBinding]
    human_authorities: dict[str, list[str]]
    retention: dict[str, int]
    team_wide_trigger: str
    permission_model: ConnectorPermissionModel

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ConnectorConfig:
        reject_inline_secrets(raw)
        required = {
            "connector_id",
            "project_id",
            "connector_type",
            "display_name",
            "project_team_ref",
            "default_project_channel_ref",
            "external_base_url",
            "role_identities",
            "retention",
            "team_wide_trigger",
        }
        missing = sorted(required - set(raw))
        if missing:
            raise ValueError(f"connector config missing required keys: {', '.join(missing)}")
        if raw["connector_type"] != "teams":
            raise ValueError("story 1 local adapter supports connector_type `teams`")
        role_identities = _role_identity_map(raw["role_identities"])
        if not role_identities:
            raise ValueError("connector config must define at least one role identity")
        default_channel_ref = _required_string(raw, "default_project_channel_ref")
        channel_bindings = _channel_binding_map(raw.get("channel_bindings", ()), default_channel_ref=default_channel_ref)
        retention = _positive_int_map(raw["retention"], "retention")
        unknown_retention = sorted(set(retention) - VALID_RETENTION_KEYS)
        if unknown_retention:
            raise ValueError(f"unknown retention keys: {', '.join(unknown_retention)}")
        return cls(
            connector_id=_required_string(raw, "connector_id"),
            project_id=_required_string(raw, "project_id"),
            connector_type=_required_string(raw, "connector_type"),
            display_name=_required_string(raw, "display_name"),
            tenant_id=str(raw["tenant_id"]).strip() if isinstance(raw.get("tenant_id"), str) and raw["tenant_id"].strip() else None,
            project_team_ref=_required_string(raw, "project_team_ref"),
            default_project_channel_ref=default_channel_ref,
            external_base_url=_required_string(raw, "external_base_url"),
            role_identities=role_identities,
            channel_bindings=channel_bindings,
            human_authorities=_authority_map(
                raw.get("human_authorities", {}),
                people=raw.get("people", ()),
                authority_groups=raw.get("authority_groups", {}),
            ),
            retention=retention,
            team_wide_trigger=_required_string(raw, "team_wide_trigger"),
            permission_model=ConnectorPermissionModel.from_dict(raw.get("permission_validation")),
        )


@dataclass(frozen=True)
class ReplayedEvent:
    receipt_id: str
    conversation_id: str
    duplicate: bool
    route_type: str
    mentioned_roles: tuple[str, ...]


class BotFrameworkDeliveryError(RuntimeError):
    def __init__(self, message: str, *, outcome: str = "failed_transient", error_class: str = "bot_framework_delivery_error") -> None:
        super().__init__(message)
        self.outcome = outcome
        self.error_class = error_class


class BotFrameworkDeliveryClient:
    def __init__(self, *, config: ConnectorConfig, secret_root: Path, timeout_seconds: int = 20) -> None:
        self.config = config
        self.secret_root = secret_root
        self.timeout_seconds = timeout_seconds
        self._token_cache: dict[str, tuple[str, float]] = {}

    def send_message(
        self,
        *,
        role_id: str,
        service_url: str,
        conversation_id: str,
        body: str,
        reply_to_id: str | None = None,
    ) -> str:
        identity = self.config.role_identities.get(role_id)
        if identity is None:
            raise BotFrameworkDeliveryError(
                f"unknown role identity `{role_id}` for Bot Framework delivery",
                outcome="failed_permanent",
                error_class="unknown_role_identity",
            )
        app_id = self._read_secret(identity.external_ref)
        if not identity.secret_ref:
            raise BotFrameworkDeliveryError(
                f"role identity `{role_id}` does not define a bot secret reference",
                outcome="failed_permanent",
                error_class="missing_bot_secret_ref",
            )
        app_secret = self._read_secret(identity.secret_ref)
        token = self._token(app_id=app_id, app_secret=app_secret)
        target_conversation_id = _teams_thread_conversation_id(conversation_id, reply_to_id)
        target_reply_to_id = None if target_conversation_id != conversation_id else reply_to_id
        endpoint = service_url.rstrip("/") + f"/v3/conversations/{urllib.parse.quote(target_conversation_id, safe='')}/activities"
        if target_reply_to_id:
            endpoint += f"/{urllib.parse.quote(target_reply_to_id, safe='')}"
        payload = {
            "type": "message",
            "text": body,
            "textFormat": "markdown",
        }
        if reply_to_id:
            payload["replyToId"] = reply_to_id
        response = self._post_json(endpoint, payload, authorization=f"Bearer {token}")
        if isinstance(response, dict):
            for key in ("id", "activityId"):
                value = response.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return f"bot-framework-message-{_stable_digest(endpoint + body)}"

    def send_personal_message(
        self,
        *,
        role_id: str,
        service_url: str,
        recipient_ref: str,
        body: str,
    ) -> str:
        identity = self.config.role_identities.get(role_id)
        if identity is None:
            raise BotFrameworkDeliveryError(
                f"unknown role identity `{role_id}` for Bot Framework delivery",
                outcome="failed_permanent",
                error_class="unknown_role_identity",
            )
        app_id = self._read_secret(identity.external_ref)
        if not identity.secret_ref:
            raise BotFrameworkDeliveryError(
                f"role identity `{role_id}` does not define a bot secret reference",
                outcome="failed_permanent",
                error_class="missing_bot_secret_ref",
            )
        app_secret = self._read_secret(identity.secret_ref)
        token = self._token(app_id=app_id, app_secret=app_secret)
        endpoint = service_url.rstrip("/") + "/v3/conversations"
        member = {"id": recipient_ref}
        if _looks_like_aad_object_id(recipient_ref):
            member["aadObjectId"] = recipient_ref
        payload: dict[str, Any] = {
            "isGroup": False,
            "bot": {"id": app_id, "name": identity.display_name},
            "members": [member],
            "activity": {
                "type": "message",
                "text": body,
                "textFormat": "markdown",
            },
        }
        if self.config.tenant_id:
            payload["channelData"] = {"tenant": {"id": self.config.tenant_id}}
        response = self._post_json(endpoint, payload, authorization=f"Bearer {token}")
        if isinstance(response, dict):
            for key in ("activityId", "id"):
                value = response.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return f"bot-framework-personal-message-{_stable_digest(endpoint + recipient_ref + body)}"

    def _read_secret(self, ref: str) -> str:
        path = self.secret_root / ref
        try:
            value = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError as exc:
            raise BotFrameworkDeliveryError(
                f"missing Teams bot secret file `{path}`",
                outcome="failed_permanent",
                error_class="missing_bot_secret",
            ) from exc
        if not value:
            raise BotFrameworkDeliveryError(
                f"Teams bot secret file `{path}` is empty",
                outcome="failed_permanent",
                error_class="empty_bot_secret",
            )
        return value

    def _token(self, *, app_id: str, app_secret: str) -> str:
        cached = self._token_cache.get(app_id)
        now = time.time()
        if cached is not None and cached[1] > now + 60:
            return cached[0]
        data = urllib.parse.urlencode(
            {
                "grant_type": "client_credentials",
                "client_id": app_id,
                "client_secret": app_secret,
                "scope": "https://api.botframework.com/.default",
            }
        ).encode("utf-8")
        authority = self.config.tenant_id or "botframework.com"
        request = urllib.request.Request(
            f"https://login.microsoftonline.com/{urllib.parse.quote(authority, safe='')}/oauth2/v2.0/token",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise BotFrameworkDeliveryError(
                f"Bot Framework token request failed with HTTP {exc.code}: {detail}",
                outcome="failed_permanent" if exc.code in {400, 401, 403} else "failed_transient",
                error_class="bot_framework_token_http_error",
            ) from exc
        except urllib.error.URLError as exc:
            raise BotFrameworkDeliveryError(
                f"Bot Framework token request failed: {exc.reason}",
                error_class="bot_framework_token_unavailable",
            ) from exc
        token = raw.get("access_token")
        if not isinstance(token, str) or not token.strip():
            raise BotFrameworkDeliveryError(
                "Bot Framework token response did not contain an access token",
                outcome="failed_permanent",
                error_class="bot_framework_token_missing",
            )
        expires_in = raw.get("expires_in")
        try:
            ttl = float(expires_in)
        except (TypeError, ValueError):
            ttl = 1800
        self._token_cache[app_id] = (token.strip(), now + ttl)
        return token.strip()

    def _post_json(self, url: str, payload: dict[str, Any], *, authorization: str) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": authorization,
                "Content-Type": "application/json; charset=utf-8",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise BotFrameworkDeliveryError(
                f"Bot Framework message send failed with HTTP {exc.code}: {detail}",
                outcome="failed_permanent" if exc.code in {400, 401, 403, 404} else "failed_transient",
                error_class="bot_framework_send_http_error",
            ) from exc
        except urllib.error.URLError as exc:
            raise BotFrameworkDeliveryError(
                f"Bot Framework message send failed: {exc.reason}",
                error_class="bot_framework_send_unavailable",
            ) from exc
        if not raw.strip():
            return {}
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}


class LocalTeamsTestAdapter:
    """Deterministic Teams-like adapter used by tests before real tenant wiring."""

    def __init__(
        self,
        db: V2Database,
        config: ConnectorConfig,
        *,
        delivery_client: BotFrameworkDeliveryClient | None = None,
    ) -> None:
        self.db = db
        self.config = config
        self.delivery_client = delivery_client

    def install(self) -> None:
        with span("v2.teams.install", connector_id=self.config.connector_id):
            self.db.upsert_connector(
                connector_id=self.config.connector_id,
                project_id=self.config.project_id,
                connector_type=self.config.connector_type,
                display_name=self.config.display_name,
                status="validating",
                health={},
            )
            permission_healthy = self.validate_startup_permissions()
            self.db.upsert_connector(
                connector_id=self.config.connector_id,
                project_id=self.config.project_id,
                connector_type=self.config.connector_type,
                display_name=self.config.display_name,
                status="configured" if permission_healthy else "permission_failed",
                health={
                    "project_team_ref": self.config.project_team_ref,
                    "default_project_channel_ref": self.config.default_project_channel_ref,
                    "identity_models": sorted({identity.identity_model for identity in self.config.role_identities.values()}),
                    "channel_bindings": [binding.__dict__ for binding in self.config.channel_bindings.values()],
                    "permission_health": "healthy" if permission_healthy else "failed",
                },
            )
            for role_id, identity in self.config.role_identities.items():
                with span("v2.teams.identity_mapping", connector_id=self.config.connector_id, role_id=role_id):
                    self.db.upsert_connector_participant(
                        participant_id=f"{self.config.connector_id}:role:{role_id}",
                        connector_id=self.config.connector_id,
                        participant_type="role",
                        display_name=identity.display_name,
                        external_ref=identity.external_ref,
                        role_id=role_id,
                        metadata={
                            "alias": identity.alias,
                            "mention_handle": identity.mention_handle,
                            "identity_model": identity.identity_model,
                            "enabled": identity.enabled,
                        },
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

    def validate_startup_permissions(self) -> bool:
        with span("v2.teams.permission_validation", connector_id=self.config.connector_id, check_type="startup"):
            capabilities = startup_capabilities_for(
                project_team_ref=self.config.project_team_ref,
                default_project_channel_ref=self.config.default_project_channel_ref,
                role_ids=sorted(self.config.role_identities),
                channel_refs=sorted(self.config.channel_bindings),
            )
            healthy = True
            for capability in capabilities:
                status = self.config.permission_model.status_for(capability)
                check_status = "pass" if status == "granted" else "fail"
                healthy = healthy and check_status == "pass"
                self.db.record_connector_permission_check(
                    check_id=f"permission-{_stable_digest(f'{self.config.connector_id}:startup:{capability}')}",
                    connector_id=self.config.connector_id,
                    check_type="startup",
                    capability=capability,
                    status=check_status,
                    required_status="granted",
                    actual_status=status,
                    phase="runtime" if capability in {"member_metadata_access", "send_capability"} else "setup",
                    next_action=_permission_next_action(capability, status),
                )
                if check_status == "fail":
                    self._permission_attention(
                        capability=capability,
                        status=status,
                        source_ref=f"startup:{capability}",
                        retryable=True,
                    )
            for declaration in self.config.permission_model.declarations:
                declaration_status = "pass"
                next_action = "No action required."
                if declaration.required and declaration.status != "granted":
                    declaration_status = "fail"
                    next_action = f"Restore or grant required {declaration.phase} permission `{declaration.permission}`."
                if declaration.broad_graph and not declaration.approval_ref:
                    declaration_status = "fail"
                    next_action = (
                        f"Document explicit security approval before using broad Graph permission "
                        f"`{declaration.permission}`."
                    )
                healthy = healthy and declaration_status == "pass"
                self.db.record_connector_permission_check(
                    check_id=f"permission-{_stable_digest(f'{self.config.connector_id}:declaration:{declaration.permission}:{declaration.phase}')}",
                    connector_id=self.config.connector_id,
                    check_type="permission",
                    capability=declaration.permission,
                    status=declaration_status,
                    required_status="granted" if declaration.required else "documented",
                    actual_status=declaration.status,
                    phase=declaration.phase,
                    consent_type=declaration.consent_type,
                    permission_name=declaration.permission,
                    required=declaration.required,
                    broad_graph=declaration.broad_graph,
                    approval_ref=declaration.approval_ref,
                    next_action=next_action,
                )
                if declaration_status == "fail":
                    self._permission_attention(
                        capability=declaration.permission,
                        status=declaration.status,
                        source_ref=f"permission:{declaration.permission}",
                        retryable=True,
                        next_action=next_action,
                    )
            return healthy

    def replay_event(self, event: dict[str, Any]) -> ReplayedEvent:
        connector_id = self.config.connector_id
        event_type = _required_string(event, "event_type")
        message_id = _required_string(event, "message_id")
        external_conversation_ref = _required_string(event, "conversation_ref")
        sender_ref = _required_string(event, "sender_ref")
        source_type = _required_string(event, "source_type")
        with span("v2.teams.receive", connector_id=connector_id, source_type=source_type):
            self._ensure_runtime_capabilities(
                runtime_capabilities_for_receive(
                    source_type=source_type,
                    conversation_ref=external_conversation_ref,
                ),
                source_ref=message_id,
            )
        channel_binding = self._channel_binding(external_conversation_ref, source_type=source_type)
        if source_type != "dm" and channel_binding is None:
            self._permission_attention(
                capability=f"channel_binding:{external_conversation_ref}",
                status="missing",
                source_ref=message_id,
                retryable=True,
                next_action="Add an explicit project, focus, or private channel binding before ingesting this Teams channel.",
            )
            self.db.record_connector_permission_check(
                check_id=f"permission-{_stable_digest(f'{self.config.connector_id}:runtime:channel_binding:{external_conversation_ref}:{message_id}')}",
                connector_id=self.config.connector_id,
                check_type="runtime",
                capability=f"channel_binding:{external_conversation_ref}",
                status="fail",
                required_status="granted",
                actual_status="missing",
                phase="runtime",
                next_action="Add an explicit project, focus, or private channel binding before ingesting this Teams channel.",
            )
            raise PermissionValidationFailure(
                f"connector permission validation failed closed: channel_binding:{external_conversation_ref}=missing",
                failures=[(f"channel_binding:{external_conversation_ref}", "missing")],
            )
        unbound_private_channel = source_type == "private_channel" and channel_binding is None
        body = str(event.get("body", ""))
        mentioned_roles = () if unbound_private_channel else self._mentioned_roles(event)
        route_type = self._route_type(source_type=source_type, mentioned_roles=mentioned_roles, body=body)
        with span("v2.teams.route_classification", connector_id=connector_id, route_type=route_type):
            pass
        if unbound_private_channel:
            route_type = "unbound_private_channel"
        visibility_scope = "private" if source_type == "dm" else "project"
        if channel_binding is not None and channel_binding.private:
            visibility_scope = "private"
        idempotency_key = self._idempotency_key(event)
        receipt_id = f"receipt-{_stable_digest(idempotency_key)}"
        with span("v2.teams.idempotency", connector_id=connector_id, external_event_id=message_id):
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
            source_type=source_type,
            sponsor_ref=sender_ref if "sponsor" in self._authority_for_human(sender_ref) else None,
        )
        self.db.upsert_connector_participant(
            participant_id=f"{connector_id}:human:{sender_ref}",
            connector_id=connector_id,
            participant_type="human",
            display_name=sender_ref,
            external_ref=sender_ref,
            authority=self._authority_for_human(sender_ref),
        )
        thread_ref = event.get("thread_ref")
        if thread_ref:
            existing_thread_binding = self.db.find_thread_binding(
                connector_id=connector_id,
                external_thread_ref=str(thread_ref),
            )
            if (
                source_type == "dm"
                and existing_thread_binding is None
                and not event.get("bound_target_ref")
            ):
                self.db.create_connector_attention_item(
                    attention_id=f"attention-{_stable_digest(f'{receipt.receipt_id}:unbound-thread')}",
                    connector_id=connector_id,
                    owner="operator",
                    reason_class="unbound_thread_reply",
                    next_action=(
                        "Inspect the private threaded reply and bind it to the correct "
                        "work item or conversation context before relying on it for flow decisions."
                    ),
                    retryable=True,
                    source_ref=receipt.receipt_id,
                )
            self.db.bind_thread(
                thread_binding_id=f"thread-{_stable_digest(f'{connector_id}:{thread_ref}')}",
                connector_id=connector_id,
                conversation_id=conversation_id,
                external_thread_ref=str(thread_ref),
                binding_type="teams_thread",
                target_ref=(
                    event.get("bound_target_ref")
                    or (existing_thread_binding or {}).get("target_ref")
                    or event.get("target_ref")
                ),
            )
        conversation_event_id = f"conversation-event-{_stable_digest(f'{receipt.receipt_id}:event')}"
        with span("v2.teams.conversation_append", connector_id=connector_id, route_type=route_type):
            self.db.record_conversation_event(
                conversation_event_id=conversation_event_id,
                conversation_id=conversation_id,
                receipt_id=receipt.receipt_id,
                connector_id=connector_id,
                event_type=route_type,
                sender_participant_id=f"{connector_id}:human:{sender_ref}",
                body_preview=body[:240],
                visibility_scope=visibility_scope,
                role_id=str(mentioned_roles[0]) if len(mentioned_roles) == 1 else None,
                thread_ref=str(thread_ref) if thread_ref else None,
                payload={
                    "source_type": source_type,
                    "message_id": message_id,
                    "service_url": event.get("service_url"),
                    "destination_ref": external_conversation_ref,
                    "destination_type": source_type,
                    "reply_to_id": event.get("reply_to_id") or message_id,
                    "mentioned_roles": list(mentioned_roles),
                    "route_type": route_type,
                    "channel_scope": channel_binding.__dict__ if channel_binding else None,
                },
            )
        if route_type == "role_direct_message":
            role_id = self._role_for_direct_message(event)
            if role_id is not None:
                with span("v2.teams.role_wake", connector_id=connector_id, role_id=role_id):
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
                            "conversation_event_id": conversation_event_id,
                            "receipt_id": receipt.receipt_id,
                            "message_id": message_id,
                            "destination_ref": external_conversation_ref,
                            "destination_type": source_type,
                            "service_url": event.get("service_url"),
                            "reply_to_id": event.get("reply_to_id") or message_id,
                            "channel_scope": channel_binding.__dict__ if channel_binding else None,
                            "context": [_conversation_context_line(source_type=source_type, sender_ref=sender_ref, body=body)],
                        },
                    )
            else:
                self.db.create_connector_attention_item(
                    attention_id=f"attention-{_stable_digest(f'{receipt.receipt_id}:unrouteable-dm')}",
                    connector_id=connector_id,
                    owner="operator",
                    reason_class="unrouteable_role_direct_message",
                    next_action=(
                        "Map the direct message target to an enabled configured role identity "
                        "or correct the connector identity binding."
                    ),
                    retryable=True,
                    source_ref=receipt.receipt_id,
                )
        if route_type == "role_mention":
            for role_id in mentioned_roles:
                with span("v2.teams.role_wake", connector_id=connector_id, role_id=role_id):
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
                            "conversation_event_id": conversation_event_id,
                            "receipt_id": receipt.receipt_id,
                            "message_id": message_id,
                            "thread_ref": thread_ref,
                            "destination_ref": external_conversation_ref,
                            "destination_type": source_type,
                            "service_url": event.get("service_url"),
                            "reply_to_id": event.get("reply_to_id") or message_id,
                            "channel_scope": channel_binding.__dict__ if channel_binding else None,
                            "context": [_conversation_context_line(source_type=source_type, sender_ref=sender_ref, body=body)],
                        },
                    )
        if route_type == "team_wide_prompt":
            for role_id, identity in self.config.role_identities.items():
                if not identity.enabled:
                    continue
                with span("v2.teams.role_wake", connector_id=connector_id, role_id=role_id):
                    self.db.create_role_assignment(
                        assignment_id=f"assignment-{_stable_digest(f'{receipt.receipt_id}:{role_id}:relevance')}",
                        role_id=role_id,
                        conversation_id=conversation_id,
                        source_ref=receipt.receipt_id,
                        title="Team-wide relevance check",
                        summary="Human asked all roles to consider whether they have material specialist input.",
                        assignment_type="team_wide_relevance_check",
                        visibility_scope="project",
                        payload={
                            "connector_id": connector_id,
                            "conversation_id": conversation_id,
                            "conversation_event_id": conversation_event_id,
                            "receipt_id": receipt.receipt_id,
                            "message_id": message_id,
                            "thread_ref": thread_ref,
                            "destination_ref": external_conversation_ref,
                            "destination_type": source_type,
                            "service_url": event.get("service_url"),
                            "reply_to_id": event.get("reply_to_id") or message_id,
                            "channel_scope": channel_binding.__dict__ if channel_binding else None,
                            "context": [_conversation_context_line(source_type=source_type, sender_ref=sender_ref, body=body)],
                            "threshold": 0.6,
                        },
                    )
        if route_type in {"unknown_role_mention", "disabled_role_identity", "unbound_private_channel"}:
            reason_class = "unknown_role_mention" if route_type == "unknown_role_mention" else "disabled_role_identity"
            if route_type == "unbound_private_channel":
                reason_class = "unbound_private_channel"
            next_action = (
                "Map the Teams mention to a configured Agentic Mesh role or correct the message."
                if route_type == "unknown_role_mention"
                else "Enable the configured role identity or route the message to an active role."
            )
            if route_type == "unbound_private_channel":
                next_action = "Add an explicit private channel binding before using this channel for project context."
            self.db.create_connector_attention_item(
                attention_id=f"attention-{_stable_digest(f'{receipt.receipt_id}:{reason_class}')}",
                connector_id=connector_id,
                owner="operator",
                reason_class=reason_class,
                next_action=next_action,
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
        outcome: str | None = None,
        service_url: str | None = None,
        reply_to_id: str | None = None,
        recipient_ref: str | None = None,
    ) -> str:
        with span("v2.teams.delivery_prepare", connector_id=self.config.connector_id, purpose=purpose):
            self._ensure_runtime_capabilities(
                runtime_capabilities_for_send(),
                source_ref=source_ref,
            )
        if role_id is not None and not self._role_enabled(role_id):
            self.db.create_connector_attention_item(
                attention_id=f"attention-{_stable_digest(f'{self.config.connector_id}:{source_ref}:{role_id}:disabled-send')}",
                connector_id=self.config.connector_id,
                owner="operator",
                reason_class="disabled_role_identity_delivery",
                next_action="Enable the role identity or route outbound delivery through an active configured role.",
                retryable=True,
                source_ref=source_ref,
            )
            raise ValueError(f"role identity `{role_id}` is disabled or not configured")
        requested_outcome = "failed_transient" if fail else outcome or "sent"
        if requested_outcome not in DELIVERY_OUTCOMES:
            raise ValueError(f"unknown delivery outcome `{requested_outcome}`")
        idempotency_key = f"{self.config.connector_id}:{source_ref}:{destination_ref}:{purpose}"
        existing = self.db.get_delivery_record_by_idempotency_key(idempotency_key)
        if existing is not None:
            return str(existing["delivery_id"])
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
            payload={
                "body": body,
                "role_identity": self._delivery_role_identity(role_id),
                "service_url": service_url,
                "reply_to_id": reply_to_id,
                "recipient_ref": recipient_ref,
            },
            status="pending",
        )
        if self.delivery_client is not None and service_url:
            self._send_live_delivery(delivery_id=delivery_id)
        else:
            self._apply_delivery_outcome(delivery_id=delivery_id, outcome=requested_outcome)
        return delivery_id

    def _send_live_delivery(self, *, delivery_id: str) -> None:
        with span("v2.teams.delivery_live", connector_id=self.config.connector_id, delivery_id=delivery_id):
            delivery = self.db.get_delivery_record(delivery_id)
            if delivery is None:
                raise ValueError(f"unknown delivery `{delivery_id}`")
            payload = _record_payload(delivery)
            self.db.update_delivery_record(delivery_id, status="sending")
            attempt_number = self.db.count_delivery_attempts(delivery_id) + 1
            external_message_id = None
            error_class = None
            error_detail = None
            outcome = "sent"
            try:
                if self.delivery_client is None:
                    raise RuntimeError("live Teams delivery client is not configured")
                recipient_ref = str(payload.get("recipient_ref") or "").strip()
                if str(delivery["destination_type"]) == "dm" and recipient_ref:
                    external_message_id = self.delivery_client.send_personal_message(
                        role_id=str(delivery["role_id"] or ""),
                        service_url=_required_string(payload, "service_url"),
                        recipient_ref=recipient_ref,
                        body=_required_string(payload, "body"),
                    )
                else:
                    external_message_id = self.delivery_client.send_message(
                        role_id=str(delivery["role_id"] or ""),
                        service_url=_required_string(payload, "service_url"),
                        conversation_id=str(delivery["destination_ref"]),
                        body=_required_string(payload, "body"),
                        reply_to_id=str(payload.get("reply_to_id") or "") or None,
                    )
            except BotFrameworkDeliveryError as exc:
                outcome = exc.outcome
                error_class = exc.error_class
                error_detail = str(exc)
            except Exception as exc:
                outcome = "failed_transient"
                error_class = exc.__class__.__name__
                error_detail = str(exc)
            self.db.record_delivery_attempt(
                attempt_id=f"delivery-attempt-{_stable_digest(f'{delivery_id}:{attempt_number}')}",
                delivery_id=delivery_id,
                connector_id=self.config.connector_id,
                attempt_number=attempt_number,
                status=outcome,
                external_message_id=external_message_id,
                error_class=error_class,
                error_detail=error_detail,
            )
            self.db.update_delivery_record(
                delivery_id,
                status=outcome,
                external_message_id=external_message_id,
                error_class=error_class,
                error_detail=error_detail,
            )
            if outcome != "sent":
                self._delivery_attention(delivery_id=delivery_id, attempt_number=attempt_number, outcome=outcome)

    def schedule_delivery_retry(self, delivery_id: str) -> None:
        with span("v2.teams.delivery_retry_schedule", connector_id=self.config.connector_id, delivery_id=delivery_id):
            delivery = self.db.get_delivery_record(delivery_id)
            if delivery is None:
                raise ValueError(f"unknown delivery `{delivery_id}`")
            if delivery["status"] == "sent":
                return
            if delivery["status"] not in RETRYABLE_DELIVERY_STATUSES:
                raise ValueError(f"delivery `{delivery_id}` is not retryable from status `{delivery['status']}`")
            self.db.update_delivery_record(delivery_id, status="retry_scheduled")

    def retry_delivery(self, delivery_id: str, *, outcome: str = "sent") -> str:
        with span("v2.teams.delivery_retry", connector_id=self.config.connector_id, delivery_id=delivery_id, outcome=outcome):
            if outcome not in DELIVERY_OUTCOMES:
                raise ValueError(f"unknown delivery outcome `{outcome}`")
            delivery = self.db.get_delivery_record(delivery_id)
            if delivery is None:
                raise ValueError(f"unknown delivery `{delivery_id}`")
            if delivery["status"] == "sent":
                return delivery_id
            if delivery["status"] not in RETRYABLE_DELIVERY_STATUSES:
                raise ValueError(f"delivery `{delivery_id}` is not retryable from status `{delivery['status']}`")
            self.schedule_delivery_retry(delivery_id)
            self._apply_delivery_outcome(delivery_id=delivery_id, outcome=outcome)
            return delivery_id

    def _apply_delivery_outcome(self, *, delivery_id: str, outcome: str) -> None:
        with span("v2.teams.delivery", connector_id=self.config.connector_id, delivery_id=delivery_id, outcome=outcome):
            delivery = self.db.get_delivery_record(delivery_id)
            if delivery is None:
                raise ValueError(f"unknown delivery `{delivery_id}`")
            self.db.update_delivery_record(delivery_id, status="sending")
            attempt_number = self.db.count_delivery_attempts(delivery_id) + 1
            external_message_id = None
            error_class = None
            error_detail = None
            if outcome == "sent":
                external_message_id = f"local-teams-message-{_stable_digest(f'{delivery_id}:{attempt_number}')}"
            elif outcome == "failed_transient":
                error_class = "simulated_transient_send_failure"
                error_detail = "Local test adapter simulated a transient Teams delivery failure."
            elif outcome == "failed_permanent":
                error_class = "simulated_permanent_send_failure"
                error_detail = "Local test adapter simulated a permanent Teams delivery failure."
            elif outcome == "unknown":
                error_class = "simulated_unknown_send_outcome"
                error_detail = "Local test adapter simulated an unknown Teams delivery outcome."
            self.db.record_delivery_attempt(
                attempt_id=f"delivery-attempt-{_stable_digest(f'{delivery_id}:{attempt_number}')}",
                delivery_id=delivery_id,
                connector_id=self.config.connector_id,
                attempt_number=attempt_number,
                status=outcome,
                external_message_id=external_message_id,
                error_class=error_class,
                error_detail=error_detail,
            )
            self.db.update_delivery_record(
                delivery_id,
                status=outcome,
                external_message_id=external_message_id,
                error_class=error_class,
                error_detail=error_detail,
            )
            if outcome in {"failed_transient", "failed_permanent", "unknown"}:
                self._delivery_attention(delivery_id=delivery_id, attempt_number=attempt_number, outcome=outcome)

    def _delivery_attention(self, *, delivery_id: str, attempt_number: int, outcome: str) -> None:
        retryable = outcome != "failed_permanent"
        reason_class = f"delivery_{outcome}"
        next_action = {
            "failed_transient": "Inspect connector delivery failure and retry when safe.",
            "failed_permanent": "Correct connector configuration or destination before retrying with a new delivery.",
            "unknown": "Check Teams for the message before retrying to avoid duplicate human-visible sends.",
        }.get(outcome, "Inspect connector delivery failure before retrying.")
        self.db.create_connector_attention_item(
            attention_id=f"attention-{_stable_digest(f'{delivery_id}:{attempt_number}:{outcome}')}",
            connector_id=self.config.connector_id,
            owner="operator",
            reason_class=reason_class,
            next_action=next_action,
            retryable=retryable,
            source_ref=delivery_id,
        )

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
            service_url=str(payload.get("service_url") or "") or None,
            reply_to_id=str(payload.get("reply_to_id") or "") or None,
            recipient_ref=str(payload.get("recipient_ref") or "") or None,
        )

    def deliver_release_notification(self, *, call_id: str, role_id: str, payload: dict[str, Any]) -> str:
        work_item_id = _required_string(payload, "work_item_id")
        _required_string(payload, "conversation_id")
        destination_ref = _required_string(payload, "destination_ref")
        destination_type = str(payload.get("destination_type") or "dm")
        message = str(
            payload.get("message")
            or payload.get("notification_message")
            or f"Release completed for `{work_item_id}`."
        )
        return self.send_message(
            source_ref=call_id,
            destination_ref=destination_ref,
            destination_type=destination_type,
            purpose="release.notification",
            body=message,
            role_id=role_id,
            outcome=str(payload.get("delivery_outcome") or "sent"),
        )

    def deliver_human_question(self, *, call_id: str, role_id: str, payload: dict[str, Any]) -> str:
        question = _required_string(payload, "question")
        conversation_id = _required_string(payload, "conversation_id")
        destination_ref = _required_string(payload, "destination_ref")
        destination_type = str(payload.get("destination_type") or "dm")
        thread_ref = str(payload.get("thread_ref") or f"thread-{call_id}")
        self.db.bind_thread(
            thread_binding_id=f"thread-{_stable_digest(f'{self.config.connector_id}:{thread_ref}:human-question')}",
            connector_id=self.config.connector_id,
            conversation_id=conversation_id,
            external_thread_ref=thread_ref,
            binding_type="human_question",
            target_ref=payload.get("work_item_id") or payload.get("target_ref") or call_id,
        )
        body = f"{question}\n\nReason: {payload.get('reason', '')}".strip()
        return self.send_message(
            source_ref=call_id,
            destination_ref=destination_ref,
            destination_type=destination_type,
            purpose="sponsor.ask_question",
            body=body,
            role_id=role_id,
            service_url=str(payload.get("service_url") or "") or None,
            recipient_ref=str(payload.get("recipient_ref") or "") or None,
        )

    def deliver_response_card(
        self,
        *,
        call_id: str,
        role_id: str,
        payload: dict[str, Any],
        request_type: str,
    ) -> str:
        title = str(
            payload.get("title")
            or ("Release approval requested" if request_type == "release_approval" else "Human response requested")
        ).strip()
        question = _required_string(payload, "question")
        conversation_id = _required_string(payload, "conversation_id")
        destination_ref = _required_string(payload, "destination_ref")
        destination_type = str(payload.get("destination_type") or "dm")
        thread_ref = str(payload.get("thread_ref") or f"thread-{call_id}")
        request_id = f"human-response-{_stable_digest(call_id)}"
        response_contract_id = str(
            payload.get("response_contract_id")
            or ("release-decision-v1" if request_type == "release_approval" else "human-response-v1")
        )
        required_authority = str(payload.get("required_authority") or "sponsor")
        card = {
            "type": "AdaptiveCard",
            "version": "1.5",
            "title": title,
            "question": question,
            "request_id": request_id,
            "request_type": request_type,
            "response_contract_id": response_contract_id,
            "required_authority": required_authority,
            "work_item_id": payload.get("work_item_id"),
            "gate_id": payload.get("gate_id"),
            "actions": [
                {"type": "Action.Submit", "title": "Approve", "data": {"value": "approve"}},
                {"type": "Action.Submit", "title": "Reject", "data": {"value": "reject"}},
                {"type": "Action.Submit", "title": "Request changes", "data": {"value": "request_changes"}},
            ],
        }
        self.db.bind_thread(
            thread_binding_id=f"thread-{_stable_digest(f'{self.config.connector_id}:{thread_ref}:human-response')}",
            connector_id=self.config.connector_id,
            conversation_id=conversation_id,
            external_thread_ref=thread_ref,
            binding_type="human_response",
            target_ref=request_id,
        )
        if self.db.get_human_response_request(request_id) is None:
            self.db.create_human_response_request(
                request_id=request_id,
                connector_id=self.config.connector_id,
                source_ref=call_id,
                request_type=request_type,
                title=title,
                question=question,
                required_authority=required_authority,
                response_contract_id=response_contract_id,
                created_by_role=role_id,
                destination_ref=destination_ref,
                destination_type=destination_type,
                work_item_id=str(payload["work_item_id"]) if payload.get("work_item_id") else None,
                gate_id=str(payload["gate_id"]) if payload.get("gate_id") else None,
                target_ref=str(payload["target_ref"]) if payload.get("target_ref") else None,
                thread_ref=thread_ref,
                payload={"card": card},
            )
        delivery_id = self.send_card(
            source_ref=call_id,
            destination_ref=destination_ref,
            destination_type=destination_type,
            purpose=f"{request_type}.card",
            card=card,
            role_id=role_id,
            service_url=str(payload.get("service_url") or "") or None,
            reply_to_id=str(payload.get("reply_to_id") or "") or None,
            recipient_ref=str(payload.get("recipient_ref") or "") or None,
            outcome=str(payload.get("delivery_outcome") or "sent"),
        )
        self.db.update_human_response_request_delivery(
            request_id=request_id,
            delivery_ref=delivery_id,
        )
        return request_id

    def send_card(
        self,
        *,
        source_ref: str,
        destination_ref: str,
        destination_type: str,
        purpose: str,
        card: dict[str, Any],
        role_id: str | None = None,
        service_url: str | None = None,
        reply_to_id: str | None = None,
        recipient_ref: str | None = None,
        outcome: str = "sent",
    ) -> str:
        with span("v2.teams.delivery_prepare", connector_id=self.config.connector_id, purpose=purpose):
            self._ensure_runtime_capabilities(
                runtime_capabilities_for_send(),
                source_ref=source_ref,
            )
        if role_id is not None and not self._role_enabled(role_id):
            self.db.create_connector_attention_item(
                attention_id=f"attention-{_stable_digest(f'{self.config.connector_id}:{source_ref}:{role_id}:disabled-card-send')}",
                connector_id=self.config.connector_id,
                owner="operator",
                reason_class="disabled_role_identity_delivery",
                next_action="Enable the role identity or route outbound card delivery through an active configured role.",
                retryable=True,
                source_ref=source_ref,
            )
            raise ValueError(f"role identity `{role_id}` is disabled or not configured")
        if outcome not in DELIVERY_OUTCOMES:
            raise ValueError(f"unknown delivery outcome `{outcome}`")
        idempotency_key = f"{self.config.connector_id}:{source_ref}:{destination_ref}:{purpose}"
        existing = self.db.get_delivery_record_by_idempotency_key(idempotency_key)
        if existing is not None:
            return str(existing["delivery_id"])
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
            payload={
                "body": _card_markdown(card),
                "card": card,
                "role_identity": self._delivery_role_identity(role_id),
                "service_url": service_url,
                "reply_to_id": reply_to_id,
                "recipient_ref": recipient_ref,
            },
            status="pending",
        )
        if self.delivery_client is not None and service_url:
            self._send_live_delivery(delivery_id=delivery_id)
        else:
            self._apply_delivery_outcome(delivery_id=delivery_id, outcome=outcome)
        return delivery_id

    def submit_card_response(
        self,
        *,
        request_id: str,
        responder_ref: str,
        response_value: str,
        comment: str | None = None,
        submission_id: str | None = None,
        update_outcome: str = "sent",
    ) -> str:
        with span("v2.teams.response_binding", connector_id=self.config.connector_id, request_id=request_id):
            self._ensure_runtime_capabilities(
                runtime_capabilities_for_response(),
                source_ref=request_id,
            )
        request = self.db.get_human_response_request(request_id)
        if request is None:
            self.db.create_connector_attention_item(
                attention_id=f"attention-{_stable_digest(f'{self.config.connector_id}:{request_id}:unknown-response-request')}",
                connector_id=self.config.connector_id,
                owner="operator",
                reason_class="unknown_response_request",
                next_action="Inspect the submitted Teams response and bind it to an active response request if appropriate.",
                retryable=False,
                source_ref=request_id,
            )
            raise ValueError(f"unknown human response request `{request_id}`")
        authority = self._authority_for_human(responder_ref)
        normalized_seed = str(response_value or "").strip().casefold().replace("-", "_").replace(" ", "_")
        resolved_submission_id = submission_id or f"human-response-submission-{_stable_digest(f'{request_id}:{responder_ref}:{normalized_seed}')}"
        if self.db.get_human_response_submission(resolved_submission_id) is not None:
            return resolved_submission_id
        try:
            normalized_value = _normalize_response_value(response_value)
        except ValueError:
            self.db.record_human_response_submission(
                submission_id=resolved_submission_id,
                request_id=request_id,
                responder_ref=responder_ref,
                response_value=response_value,
                normalized_value="invalid",
                status="rejected_invalid",
                authority=authority,
                comment=comment,
            )
            self.db.create_connector_attention_item(
                attention_id=f"attention-{_stable_digest(f'{self.config.connector_id}:{resolved_submission_id}:invalid-card')}",
                connector_id=self.config.connector_id,
                owner="operator",
                reason_class="invalid_card_submission",
                next_action="Ask the responder to use one of the configured response actions or inspect the card payload.",
                retryable=True,
                source_ref=resolved_submission_id,
            )
            return resolved_submission_id
        required_authority = str(request["required_authority"])
        if request["status"] != "awaiting_response":
            self.db.record_human_response_submission(
                submission_id=resolved_submission_id,
                request_id=request_id,
                responder_ref=responder_ref,
                response_value=response_value,
                normalized_value=normalized_value,
                status="rejected_stale",
                authority=authority,
                comment=comment,
            )
            self.db.create_connector_attention_item(
                attention_id=f"attention-{_stable_digest(f'{self.config.connector_id}:{resolved_submission_id}:stale-card')}",
                connector_id=self.config.connector_id,
                owner="operator",
                reason_class="stale_card_submission",
                next_action="Tell the responder the card has already been answered or superseded and point them to the current request.",
                retryable=False,
                source_ref=resolved_submission_id,
            )
            return resolved_submission_id
        if required_authority not in authority:
            self.db.record_human_response_submission(
                submission_id=resolved_submission_id,
                request_id=request_id,
                responder_ref=responder_ref,
                response_value=response_value,
                normalized_value=normalized_value,
                status="rejected_unauthorized",
                authority=authority,
                comment=comment,
            )
            self.db.create_connector_attention_item(
                attention_id=f"attention-{_stable_digest(f'{self.config.connector_id}:{resolved_submission_id}:unauthorized-card')}",
                connector_id=self.config.connector_id,
                owner="operator",
                reason_class="unauthorized_card_submission",
                next_action="Review the submitted response and update human authority mapping if the responder should be allowed to decide.",
                retryable=False,
                source_ref=resolved_submission_id,
            )
            return resolved_submission_id
        self.db.record_human_response_submission(
            submission_id=resolved_submission_id,
            request_id=request_id,
            responder_ref=responder_ref,
            response_value=response_value,
            normalized_value=normalized_value,
            status="accepted",
            authority=authority,
            comment=comment,
        )
        self.db.complete_human_response_request(
            request_id=request_id,
            response_value=normalized_value,
            responder_ref=responder_ref,
        )
        self._create_human_response_followup_assignment(
            request=request,
            submission_id=resolved_submission_id,
            responder_ref=responder_ref,
            normalized_value=normalized_value,
            comment=comment,
        )
        card = {
            "type": "AdaptiveCard",
            "version": "1.5",
            "title": f"{request['title']} - response recorded",
            "request_id": request_id,
            "status": "responded",
            "response": normalized_value,
            "responder_ref": responder_ref,
        }
        update_ref = self.send_card(
            source_ref=resolved_submission_id,
            destination_ref=str(request["destination_ref"]),
            destination_type=str(request["destination_type"]),
            purpose="card.update",
            card=card,
            role_id=str(request["created_by_role"]),
            outcome=update_outcome,
        )
        self.db.update_human_response_request_delivery(
            request_id=request_id,
            card_update_ref=update_ref,
        )
        return resolved_submission_id

    def _create_human_response_followup_assignment(
        self,
        *,
        request: dict[str, Any],
        submission_id: str,
        responder_ref: str,
        normalized_value: str,
        comment: str | None,
    ) -> None:
        assignment_id = f"assignment-{_stable_digest(f'{submission_id}:followup')}"
        if self.db.get_role_assignment(assignment_id) is not None:
            return
        role_id = str(request["created_by_role"])
        request_type = str(request["request_type"])
        private = request.get("destination_type") == "dm"
        title = f"Human response received: {request['title']}"
        summary = (
            f"{responder_ref} responded `{normalized_value}` to "
            f"{request_type} request {request['request_id']}."
        )
        self.db.create_role_assignment(
            assignment_id=assignment_id,
            role_id=role_id,
            work_item_id=str(request["work_item_id"]) if request.get("work_item_id") else None,
            source_ref=submission_id,
            title=title,
            summary=summary,
            assignment_type="human_response_followup",
            visibility_scope="private" if private else "project",
            payload={
                "source_ref": submission_id,
                "request_id": request["request_id"],
                "request_type": request_type,
                "question": request["question"],
                "response_contract_id": request["response_contract_id"],
                "response_value": normalized_value,
                "submission_comment": comment,
                "responder_ref": responder_ref,
                "required_authority": request["required_authority"],
                "work_item_id": request.get("work_item_id"),
                "gate_id": request.get("gate_id"),
                "target_ref": request.get("target_ref"),
                "thread_ref": request.get("thread_ref"),
                "allowed_tools": sorted(SafeOutputService(self.db).policy.tools_for_role(role_id)),
            },
        )

    def record_relevance(self, *, call_id: str, role_id: str, payload: dict[str, Any]) -> None:
        with span("v2.teams.relevance", connector_id=self.config.connector_id, role_id=role_id):
            conversation_event_id = _required_string(payload, "conversation_event_id")
            decision = _required_string(payload, "decision")
            if decision not in {"material", "not_relevant", "exception"}:
                raise ValueError(f"unknown relevance decision `{decision}`")
            score = _float_field(payload, "score")
            threshold = _float_field(payload, "threshold")
            noop = _bool_field(payload, "noop", default=decision == "not_relevant")
            self.db.record_relevance_check(
                relevance_check_id=f"relevance-{_stable_digest(f'{conversation_event_id}:{role_id}')}",
                conversation_event_id=conversation_event_id,
                role_id=role_id,
                score=score,
                threshold=threshold,
                decision=decision,
                reason=_required_string(payload, "reason"),
                noop=noop,
                exception_reason=str(payload["exception_reason"]) if payload.get("exception_reason") else None,
                safe_output_ref=call_id,
                delivery_ref=str(payload["delivery_ref"]) if payload.get("delivery_ref") else None,
            )

    def _ensure_runtime_capabilities(self, capabilities: list[str], *, source_ref: str) -> None:
        with span("v2.teams.permission_validation", connector_id=self.config.connector_id, check_type="runtime"):
            failures = self.config.permission_model.failing_capabilities(capabilities)
        if not failures:
            return
        for capability, status in failures:
            self.db.record_connector_permission_check(
                check_id=f"permission-{_stable_digest(f'{self.config.connector_id}:runtime:{capability}:{source_ref}')}",
                connector_id=self.config.connector_id,
                check_type="runtime",
                capability=capability,
                status="fail",
                required_status="granted",
                actual_status=status,
                phase="runtime",
                next_action=_permission_next_action(capability, status),
            )
            self._permission_attention(
                capability=capability,
                status=status,
                source_ref=source_ref,
                retryable=True,
            )
        joined = ", ".join(f"{capability}={status}" for capability, status in failures)
        raise PermissionValidationFailure(
            f"connector permission validation failed closed: {joined}",
            failures=failures,
        )

    def _permission_attention(
        self,
        *,
        capability: str,
        status: str,
        source_ref: str,
        retryable: bool,
        next_action: str | None = None,
    ) -> None:
        self.db.create_connector_attention_item(
            attention_id=f"attention-{_stable_digest(f'{self.config.connector_id}:permission:{capability}:{source_ref}')}",
            connector_id=self.config.connector_id,
            owner="operator",
            reason_class="connector_permission_failed",
            next_action=next_action or _permission_next_action(capability, status),
            retryable=retryable,
            source_ref=source_ref,
        )

    def _authority_for_human(self, external_ref: str) -> list[str]:
        participant = self.db.get_connector_participant_by_external_ref(
            connector_id=self.config.connector_id,
            external_ref=external_ref,
        )
        if participant is not None and participant.get("participant_type") == "human":
            authority = participant.get("authority")
            if isinstance(authority, list):
                return [str(item) for item in authority]
        return list(self.config.human_authorities.get(external_ref, []))

    def _route_type(self, *, source_type: str, mentioned_roles: tuple[str, ...], body: str) -> str:
        if source_type == "dm":
            return "role_direct_message"
        if self.config.team_wide_trigger and self.config.team_wide_trigger in body:
            return "team_wide_prompt"
        if mentioned_roles:
            unknown = [role for role in mentioned_roles if role not in self.config.role_identities]
            if unknown:
                return "unknown_role_mention"
            disabled = [role for role in mentioned_roles if not self.config.role_identities[role].enabled]
            if disabled:
                return "disabled_role_identity"
            return "role_mention"
        return "project_channel_context"

    def _channel_binding(self, conversation_ref: str, *, source_type: str) -> ChannelBinding | None:
        if source_type == "dm":
            return None
        binding = self.config.channel_bindings.get(conversation_ref)
        if source_type == "private_channel" and binding is None:
            return None
        if binding is not None:
            return binding
        if conversation_ref == self.config.default_project_channel_ref:
            return ChannelBinding(
                channel_ref=conversation_ref,
                scope_type="project",
                display_name="Project",
                visibility="project",
                work_scope=None,
                private=False,
            )
        return None

    def _role_for_direct_message(self, event: dict[str, Any]) -> str | None:
        role_id = event.get("target_role_id")
        if isinstance(role_id, str) and self._role_enabled(role_id):
            return role_id
        if isinstance(role_id, str):
            return None
        target_ref = event.get("target_ref")
        if isinstance(target_ref, str):
            for configured_role_id, identity in self.config.role_identities.items():
                if target_ref == identity.external_ref and identity.enabled:
                    return configured_role_id
            return None
        enabled_roles = [role for role, identity in self.config.role_identities.items() if identity.enabled]
        if len(enabled_roles) == 1:
            return enabled_roles[0]
        return None

    def _mentioned_roles(self, event: dict[str, Any]) -> tuple[str, ...]:
        explicit = [str(role) for role in event.get("mentioned_roles", ())]
        refs = [str(ref) for ref in event.get("mentioned_role_refs", ())]
        resolved = list(explicit)
        target_role_id = event.get("target_role_id")
        if isinstance(target_role_id, str) and target_role_id.strip():
            resolved.append(target_role_id.strip())
        target_ref = event.get("target_ref")
        if isinstance(target_ref, str) and target_ref.strip():
            role_id = self._role_for_mention_ref(target_ref.strip())
            if role_id is not None:
                resolved.append(role_id)
        for ref in refs:
            resolved.append(self._role_for_mention_ref(ref) or f"unknown:{ref}")
        return tuple(dict.fromkeys(resolved))

    def _role_for_mention_ref(self, ref: str) -> str | None:
        for role_id, identity in self.config.role_identities.items():
            if ref in {identity.external_ref, identity.mention_handle, identity.alias}:
                return role_id
        return None

    def _role_enabled(self, role_id: str) -> bool:
        identity = self.config.role_identities.get(role_id)
        return bool(identity and identity.enabled)

    def _delivery_role_identity(self, role_id: str | None) -> dict[str, Any] | None:
        if role_id is None:
            return None
        identity = self.config.role_identities.get(role_id)
        if identity is None:
            return None
        return {
            "role_id": role_id,
            "external_ref": identity.external_ref,
            "display_name": identity.display_name,
            "alias": identity.alias,
            "mention_handle": identity.mention_handle,
            "identity_model": identity.identity_model,
            "enabled": identity.enabled,
        }

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
        release_service: ReleaseService | None = None,
        process_effects: bool = True,
        document_library_root: Any | None = None,
        document_framework: Any | None = None,
        project_id: str | None = None,
        role_memory_path_resolver: Any | None = None,
    ) -> None:
        super().__init__(
            db,
            policy,
            release_service=release_service,
            process_effects=process_effects,
            document_library_root=document_library_root,
            document_framework=document_framework,
            project_id=project_id,
            role_memory_path_resolver=role_memory_path_resolver,
        )
        self.adapter = adapter

    def record(self, *, run_id: str, call: SafeOutputCall) -> str:
        if call.tool_name in {
            "sponsor.ask_question",
            "product.mark_sponsor_ready",
            "human_response.request",
            "release.request_approval",
        }:
            self._enrich_human_destination_payload(run_id=run_id, payload=call.payload)
        if call.tool_name == "status.reply":
            self._reject_noop_relevance_reply(run_id=run_id, role_id=call.role_id)
            self._validate_reply_references(call.payload)
        if call.tool_name in {"release.close", "work_item.close"} and "conversation_id" in call.payload:
            self._validate_release_notification_payload(call.payload)
        if call.tool_name == "queue.propose_item":
            self._validate_work_proposal_source(call.payload)
        if call.tool_name in {"human_response.request", "release.request_approval"}:
            self._validate_response_card_payload(call.payload)
            call.payload.setdefault("connector_id", self.adapter.config.connector_id)
        return super().record(run_id=run_id, call=call)

    def process_recorded_call(self, *, call_id: str, run_id: str, call: SafeOutputCall) -> None:
        close_pre_state: str | None = None
        if call.tool_name in {"release.close", "work_item.close"} and "conversation_id" in call.payload:
            self._validate_release_notification_payload(call.payload)
            close_pre_state = self.db.get_work_item(_required_string(call.payload, "work_item_id")).state
        super().process_recorded_call(call_id=call_id, run_id=run_id, call=call)
        if call.tool_name == "relevance.record":
            self.adapter.record_relevance(call_id=call_id, role_id=call.role_id, payload=call.payload)
        if call.tool_name == "queue.propose_item":
            self._validate_work_proposal_source(call.payload)
            self._record_work_proposal(call_id=call_id, role_id=call.role_id, payload=call.payload)
        if call.tool_name == "status.reply":
            self._enrich_conversation_delivery_payload(run_id=run_id, payload=call.payload)
        if call.tool_name == "status.reply" and "conversation_id" in call.payload:
            self._reject_noop_relevance_reply(run_id=run_id, role_id=call.role_id)
            self._validate_reply_references(call.payload)
            self.adapter.deliver_status_reply(
                call_id=call_id,
                role_id=call.role_id,
                payload=call.payload,
            )
        if call.tool_name == "sponsor.ask_question" and "conversation_id" in call.payload:
            self.adapter.deliver_human_question(
                call_id=call_id,
                role_id=call.role_id,
                payload=call.payload,
            )
        if call.tool_name == "human_response.request" and "conversation_id" in call.payload:
            self._validate_response_card_payload(call.payload)
            self.adapter.deliver_response_card(
                call_id=call_id,
                role_id=call.role_id,
                payload=call.payload,
                request_type="human_response",
            )
        if call.tool_name == "release.request_approval" and "conversation_id" in call.payload:
            self._validate_response_card_payload(call.payload)
            self.adapter.deliver_response_card(
                call_id=call_id,
                role_id=call.role_id,
                payload=call.payload,
                request_type="release_approval",
            )
        if call.tool_name == "product.mark_sponsor_ready" and "conversation_id" in call.payload:
            self._validate_response_card_payload(call.payload)
            self.adapter.deliver_response_card(
                call_id=call_id,
                role_id=call.role_id,
                payload=self._product_signoff_card_payload(call),
                request_type="product_signoff",
            )
        if call.tool_name in {"release.close", "work_item.close"} and "conversation_id" in call.payload:
            self._validate_release_notification_payload(call.payload)
            close_post_state = self.db.get_work_item(_required_string(call.payload, "work_item_id")).state
            if close_pre_state != "closed" and close_post_state == "closed":
                self.adapter.deliver_release_notification(
                    call_id=call_id,
                    role_id=call.role_id,
                    payload=call.payload,
                )

    def _reject_noop_relevance_reply(self, *, run_id: str, role_id: str) -> None:
        for check in self.adapter.db.list_relevance_checks_for_run(run_id, role_id):
            if check.get("noop") is True:
                raise ValueError(
                    "role recorded a no-op relevance decision in this run; it must not post a Teams reply"
                )

    def _validate_reply_references(self, payload: dict[str, Any]) -> None:
        queue_item_id = payload.get("queue_item_id")
        if queue_item_id is not None and self.db.get_queue_item(str(queue_item_id)) is None:
            raise ValueError(f"status.reply referenced unknown queue item `{queue_item_id}`")
        work_item_id = payload.get("work_item_id")
        if work_item_id is not None:
            try:
                self.db.get_work_item(str(work_item_id))
            except ValueError as exc:
                raise ValueError(f"status.reply referenced unknown work item `{work_item_id}`") from exc

    def _enrich_conversation_delivery_payload(self, *, run_id: str, payload: dict[str, Any]) -> None:
        for assignment in self.db.list_role_assignments():
            if assignment.get("run_id") != run_id:
                continue
            assignment_payload = assignment.get("payload")
            if not isinstance(assignment_payload, dict):
                raw_payload = assignment.get("payload_json")
                if isinstance(raw_payload, str):
                    try:
                        assignment_payload = json.loads(raw_payload)
                    except json.JSONDecodeError:
                        assignment_payload = {}
            if not isinstance(assignment_payload, dict):
                return
            for key in ("conversation_id", "destination_ref", "destination_type", "service_url", "reply_to_id"):
                value = assignment_payload.get(key)
                if value and not payload.get(key):
                    payload[key] = value
            return

    def _enrich_human_destination_payload(self, *, run_id: str, payload: dict[str, Any]) -> None:
        source_run = self.db.get_agent_run(run_id)
        for assignment in self.db.list_role_assignments():
            if not _assignment_matches_run_context(assignment, run_id=run_id, source_run=source_run):
                continue
            assignment_payload = assignment.get("payload")
            if not isinstance(assignment_payload, dict):
                raw_payload = assignment.get("payload_json")
                if isinstance(raw_payload, str):
                    try:
                        assignment_payload = json.loads(raw_payload)
                    except json.JSONDecodeError:
                        assignment_payload = {}
            if not isinstance(assignment_payload, dict):
                return
            for key in ("conversation_id", "service_url", "reply_to_id", "thread_ref"):
                value = assignment_payload.get(key)
                if value and not payload.get(key):
                    payload[key] = value
            destination_ref = assignment_payload.get("destination_ref")
            if destination_ref and (
                not payload.get("destination_ref") or str(payload.get("destination_ref")) == "sponsor"
            ):
                payload["destination_ref"] = destination_ref
            destination_type = assignment_payload.get("destination_type")
            if destination_type and (
                not payload.get("destination_type") or str(payload.get("destination_type")) == "runtime"
            ):
                payload["destination_type"] = destination_type
            if not payload.get("connector_id"):
                payload["connector_id"] = self.adapter.config.connector_id
            self._prefer_human_dm_destination(
                payload=payload,
                assignment=assignment,
                assignment_payload=assignment_payload,
            )
            return

    def _prefer_human_dm_destination(
        self,
        *,
        payload: dict[str, Any],
        assignment: dict[str, Any],
        assignment_payload: dict[str, Any],
    ) -> None:
        if str(payload.get("destination_type") or "") == "dm":
            if payload.get("recipient_ref"):
                return
            recipient_ref = _sponsor_ref_from_assignment_payload(self.db, assignment_payload)
            if recipient_ref:
                payload["recipient_ref"] = recipient_ref
                if not payload.get("destination_ref") or str(payload.get("destination_ref")) in {"sponsor", "runtime"}:
                    payload["destination_ref"] = _logical_dm_ref(
                        connector_id=self.adapter.config.connector_id,
                        role_id=str(assignment.get("role_id") or assignment_payload.get("target_role") or ""),
                        recipient_ref=recipient_ref,
                    )
                payload.pop("reply_to_id", None)
            return
        recipient_ref = _sponsor_ref_from_assignment_payload(self.db, assignment_payload)
        if not recipient_ref:
            return
        payload["recipient_ref"] = recipient_ref
        payload["destination_type"] = "dm"
        payload["destination_ref"] = _logical_dm_ref(
            connector_id=self.adapter.config.connector_id,
            role_id=str(assignment.get("role_id") or assignment_payload.get("target_role") or ""),
            recipient_ref=recipient_ref,
        )
        payload.pop("reply_to_id", None)

    def _product_signoff_card_payload(self, call: SafeOutputCall) -> dict[str, Any]:
        payload = dict(call.payload)
        work_item_id = _required_string(payload, "work_item_id")
        work_item = self.db.get_work_item(work_item_id)
        summary = str(payload.get("summary") or "").strip()
        payload.setdefault("title", f"Product sign-off: {work_item.title}")
        payload.setdefault(
            "question",
            f"Product shaping is ready for sponsor sign-off. {summary}",
        )
        payload.setdefault("response_contract_id", "product-signoff-v1")
        payload.setdefault("required_authority", "sponsor")
        payload.setdefault("gate_id", "product_signoff")
        payload.setdefault("work_item_id", work_item_id)
        return payload

    def _validate_work_proposal_source(self, payload: dict[str, Any]) -> None:
        conversation_event_id = payload.get("source_conversation_event_id")
        if isinstance(conversation_event_id, str) and conversation_event_id.strip():
            if self.db.get_conversation_event(conversation_event_id) is None:
                raise ValueError(f"unknown source conversation event `{conversation_event_id}`")

    def _validate_response_card_payload(self, payload: dict[str, Any]) -> None:
        work_item_id = payload.get("work_item_id")
        if work_item_id is not None:
            try:
                self.db.get_work_item(str(work_item_id))
            except ValueError as exc:
                raise ValueError(f"response request referenced unknown work item `{work_item_id}`") from exc
        if "conversation_id" in payload:
            _required_string(payload, "destination_ref")

    def _validate_release_notification_payload(self, payload: dict[str, Any]) -> None:
        self.db.get_work_item(_required_string(payload, "work_item_id"))
        conversation_id = _required_string(payload, "conversation_id")
        destination_ref = _required_string(payload, "destination_ref")
        destination_type = str(payload.get("destination_type") or "dm")
        conversation = self.db.get_conversation(conversation_id)
        if conversation is None:
            raise ValueError(f"release notification referenced unknown conversation `{conversation_id}`")
        if conversation.get("connector") != self.adapter.config.connector_id:
            raise ValueError(
                f"release notification conversation `{conversation_id}` does not belong to connector `{self.adapter.config.connector_id}`"
            )
        if conversation.get("external_ref") != destination_ref:
            raise ValueError(
                f"release notification destination `{destination_ref}` does not match conversation `{conversation_id}`"
            )
        source_type = str(conversation.get("source_type") or "unknown")
        if source_type == "dm" and destination_type != "dm":
            raise ValueError(
                f"release notification destination type `{destination_type}` does not match DM conversation `{conversation_id}`"
            )
        if source_type in {"channel", "private_channel"} and destination_type == "dm":
            raise ValueError(
                f"release notification destination type `dm` does not match {source_type} conversation `{conversation_id}`"
            )

    def _record_work_proposal(self, *, call_id: str, role_id: str, payload: dict[str, Any]) -> None:
        source_ref = _required_string(payload, "source_ref")
        conversation_event_id = payload.get("source_conversation_event_id")
        event = None
        if isinstance(conversation_event_id, str) and conversation_event_id.strip():
            event = self.db.get_conversation_event(conversation_event_id)
            if event is None:
                raise ValueError(f"unknown source conversation event `{conversation_event_id}`")
        source_conversation_id = (
            str(payload["source_conversation_id"])
            if isinstance(payload.get("source_conversation_id"), str) and payload.get("source_conversation_id")
            else (str(event["conversation_id"]) if event else None)
        )
        source_receipt_id = (
            str(payload["source_receipt_id"])
            if isinstance(payload.get("source_receipt_id"), str) and payload.get("source_receipt_id")
            else (str(event["receipt_id"]) if event and event.get("receipt_id") else None)
        )
        redaction = str(payload.get("redaction") or "").strip()
        if not redaction:
            redaction = (
                "private_source_redacted"
                if event and event.get("visibility_scope") == "private"
                else "project_context"
            )
        queue_item_id = f"queue-{_stable_digest(f'{call_id}:queue')}"
        owner_role = _required_string(payload, "suggested_owner")
        if self.db.get_queue_item(queue_item_id) is None:
            self.db.create_queue_item(
                queue_item_id=queue_item_id,
                title=_required_string(payload, "title"),
                summary=_required_string(payload, "summary"),
                owner_role=owner_role,
                source_kind="conversation",
                source_ref=source_ref,
            )
        if self.db.get_work_proposal_by_safe_output_ref(call_id) is not None:
            return
        self.db.record_work_proposal(
            proposal_id=f"proposal-{_stable_digest(f'{queue_item_id}:{call_id}')}",
            queue_item_id=queue_item_id,
            source_conversation_id=source_conversation_id,
            source_conversation_event_id=str(conversation_event_id) if conversation_event_id else None,
            source_receipt_id=source_receipt_id,
            source_ref=source_ref,
            proposed_by_role=role_id,
            initiated_by=_required_string(payload, "initiated_by"),
            classification=_required_string(payload, "classification"),
            redaction=redaction,
            target_artifact=str(payload["target_artifact"]) if payload.get("target_artifact") else None,
            rationale=_required_string(payload, "rationale"),
            urgency=_required_string(payload, "urgency"),
            suggested_owner=owner_role,
            work_type=_required_string(payload, "work_type"),
            safe_output_ref=call_id,
        )


def _required_string(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"`{key}` must be a non-empty string")
    return value


def _float_field(raw: dict[str, Any], key: str) -> float:
    value = raw.get(key)
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"`{key}` must be a number") from exc
    if parsed < 0 or parsed > 1:
        raise ValueError(f"`{key}` must be between 0 and 1")
    return parsed


def _bool_field(raw: dict[str, Any], key: str, *, default: bool) -> bool:
    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"`{key}` must be a boolean")
    return value


def _normalize_response_value(value: str) -> str:
    normalized = str(value or "").strip().casefold().replace("-", "_").replace(" ", "_")
    aliases = {
        "approved": "approve",
        "approval": "approve",
        "yes": "approve",
        "rejected": "reject",
        "no": "reject",
        "changes": "request_changes",
        "change_request": "request_changes",
        "request_change": "request_changes",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in {"approve", "reject", "request_changes"}:
        raise ValueError(f"unknown response value `{value}`")
    return normalized


def _conversation_context_line(*, source_type: str, sender_ref: str, body: str) -> str:
    surface = "Teams DM" if source_type == "dm" else "Teams channel"
    return f"{surface} message from {sender_ref}: {body}".strip()


def _permission_next_action(capability: str, status: str) -> str:
    if status == "revoked":
        return (
            f"Connector capability `{capability}` was revoked. Restore consent, installation, "
            "or binding before processing Teams traffic."
        )
    return (
        f"Connector capability `{capability}` is missing. Complete setup/admin consent "
        "or correct the project/team/channel/role binding before retrying."
    )


def _record_payload(record: dict[str, Any]) -> dict[str, Any]:
    payload = record.get("payload")
    if isinstance(payload, dict):
        return payload
    payload_json = record.get("payload_json")
    if isinstance(payload_json, str) and payload_json.strip():
        value = json.loads(payload_json)
        if isinstance(value, dict):
            return value
    return {}


def _teams_thread_conversation_id(conversation_id: str, reply_to_id: str | None) -> str:
    if not reply_to_id:
        return conversation_id
    if ";messageid=" in conversation_id.casefold():
        return conversation_id
    if "@thread.tacv2" not in conversation_id:
        return conversation_id
    return f"{conversation_id};messageid={reply_to_id}"


def _card_markdown(card: dict[str, Any]) -> str:
    lines: list[str] = []
    title = str(card.get("title") or "").strip()
    if title:
        lines.append(f"**{title}**")
    question = str(card.get("question") or "").strip()
    if question:
        if lines:
            lines.append("")
        lines.append(question)
    work_item_id = str(card.get("work_item_id") or "").strip()
    if work_item_id:
        lines.append("")
        lines.append(f"Work item: `{work_item_id}`")
    request_id = str(card.get("request_id") or "").strip()
    if request_id:
        lines.append(f"Response request: `{request_id}`")
    response_contract_id = str(card.get("response_contract_id") or "").strip()
    if response_contract_id:
        lines.append(f"Response contract: `{response_contract_id}`")
    return "\n".join(lines).strip()


def _assignment_matches_run_context(
    assignment: dict[str, Any],
    *,
    run_id: str,
    source_run: dict[str, Any] | None,
) -> bool:
    if assignment.get("run_id") == run_id:
        return True
    if source_run is None:
        return False
    if assignment.get("status") != "claimed":
        return False
    if assignment.get("role_id") != source_run.get("role_id"):
        return False
    if assignment.get("role_instance_id") != source_run.get("role_instance_id"):
        return False
    run_work_item_id = source_run.get("work_item_id")
    assignment_work_item_id = assignment.get("work_item_id")
    return (
        run_work_item_id is None
        or assignment_work_item_id is None
        or assignment_work_item_id == run_work_item_id
    )


def _sponsor_ref_from_assignment_payload(db: V2Database, assignment_payload: dict[str, Any]) -> str | None:
    conversation_event_id = assignment_payload.get("conversation_event_id")
    if not isinstance(conversation_event_id, str) or not conversation_event_id.strip():
        return None
    event = db.get_conversation_event(conversation_event_id.strip())
    if event is None:
        return None
    sender = str(event.get("sender_participant_id") or "")
    marker = ":human:"
    if marker not in sender:
        return None
    sponsor_ref = sender.split(marker, 1)[1].strip()
    return sponsor_ref or None


def _logical_dm_ref(*, connector_id: str, role_id: str, recipient_ref: str) -> str:
    return f"dm-{_stable_digest(f'{connector_id}:{role_id}:{recipient_ref}')}"


def _looks_like_aad_object_id(value: str) -> bool:
    parts = value.split("-")
    return len(parts) == 5 and all(part for part in parts)


def _role_identity_map(value: object) -> dict[str, RoleIdentity]:
    if not isinstance(value, dict):
        raise ValueError("`role_identities` must be a mapping")
    result: dict[str, RoleIdentity] = {}
    seen_external_refs: set[str] = set()
    for role_id, item in value.items():
        if not isinstance(role_id, str) or not role_id.strip():
            raise ValueError("`role_identities` keys must be non-empty role ids")
        if not isinstance(item, dict):
            raise ValueError("`role_identities` values must be identity objects")
        external_ref = _required_string(item, "external_ref")
        if external_ref in seen_external_refs:
            raise ValueError("role identity external_ref values must be unique")
        seen_external_refs.add(external_ref)
        identity_model = _required_string(item, "identity_model")
        if identity_model not in IDENTITY_MODELS:
            raise ValueError(f"unknown role identity model `{identity_model}`")
        enabled = item.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("role identity `enabled` must be a boolean")
        result[role_id] = RoleIdentity(
            role_id=role_id,
            external_ref=external_ref,
            secret_ref=str(item["secret_ref"]).strip() if isinstance(item.get("secret_ref"), str) and item["secret_ref"].strip() else None,
            display_name=_required_string(item, "display_name"),
            alias=_required_string(item, "alias"),
            mention_handle=_required_string(item, "mention_handle"),
            identity_model=identity_model,
            enabled=enabled,
        )
    return result


def _channel_binding_map(value: object, *, default_channel_ref: str) -> dict[str, ChannelBinding]:
    if value in (None, ()):
        return {}
    if not isinstance(value, list):
        raise ValueError("`channel_bindings` must be a list")
    result: dict[str, ChannelBinding] = {}
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("`channel_bindings` entries must be objects")
        channel_ref = _required_string(item, "channel_ref")
        if channel_ref == default_channel_ref:
            raise ValueError("focused channel bindings must not duplicate the default project channel")
        if channel_ref in result:
            raise ValueError("channel binding channel_ref values must be unique")
        scope_type = _required_string(item, "scope_type")
        if scope_type not in {"feature", "epic", "incident", "focused_work"}:
            raise ValueError(f"unknown channel binding scope_type `{scope_type}`")
        visibility = _required_string(item, "visibility")
        if visibility not in {"project", "restricted", "private"}:
            raise ValueError(f"unknown channel binding visibility `{visibility}`")
        private = item.get("private", visibility == "private")
        if not isinstance(private, bool):
            raise ValueError("channel binding `private` must be a boolean")
        result[channel_ref] = ChannelBinding(
            channel_ref=channel_ref,
            scope_type=scope_type,
            display_name=_required_string(item, "display_name"),
            visibility=visibility,
            work_scope=str(item["work_scope"]) if item.get("work_scope") is not None else None,
            private=private,
        )
    return result


def _authority_map(
    value: object,
    *,
    people: object = (),
    authority_groups: object = {},
) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        raise ValueError("`human_authorities` must be a mapping")
    result: dict[str, list[str]] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip() or not isinstance(item, list):
            raise ValueError("`human_authorities` must map humans to authority lists")
        result[key] = _unique_strings(item)
    groups = _authority_group_map(authority_groups)
    if people not in (None, ()):
        if not isinstance(people, list):
            raise ValueError("`people` must be a list")
        for person in people:
            if not isinstance(person, dict):
                raise ValueError("`people` entries must be objects")
            refs = person.get("external_refs")
            if not isinstance(refs, list) or not refs:
                raise ValueError("people entries must define non-empty `external_refs`")
            direct = _unique_strings(person.get("authorities", []))
            group_authorities: list[str] = []
            for group in _unique_strings(person.get("groups", [])):
                group_authorities.extend(groups.get(group, []))
            authorities = _unique_strings([*direct, *group_authorities])
            for ref in _unique_strings(refs):
                result[ref] = authorities
    return result


def _authority_group_map(value: object) -> dict[str, list[str]]:
    if value in (None, ()):
        return {}
    if not isinstance(value, dict):
        raise ValueError("`authority_groups` must be a mapping")
    result: dict[str, list[str]] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip() or not isinstance(item, list):
            raise ValueError("`authority_groups` must map group ids to authority lists")
        result[key] = _unique_strings(item)
    return result


def _unique_strings(value: object) -> list[str]:
    if value in (None, ()):
        return []
    if not isinstance(value, list):
        raise ValueError("authority values must be lists")
    result: list[str] = []
    for item in value:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
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
