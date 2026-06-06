from __future__ import annotations

import json
import hashlib
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib import request
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.parse import urlparse
from urllib.parse import urlencode
import html

from agentic_mesh import telemetry
from agentic_mesh.approval_decisions import ApprovalDecisionViewModel
from agentic_mesh.approval_decisions import build_recorded_approval_decision
from agentic_mesh.approval_decisions import build_requested_approval_decision
from agentic_mesh.journal import EventJournal
from agentic_mesh.human_gates import FileHumanGateRequestStore
from agentic_mesh.human_response_submissions import HumanResponseSubmissionService
from agentic_mesh.human_response_submissions import HumanResponseSubmissionResult
from agentic_mesh.messaging import MESSAGE_TYPE_HUMAN_RESPONSE_REQUESTED
from agentic_mesh.messaging import MESSAGE_TYPE_PROBLEM_STATUS_UPDATED
from agentic_mesh.messaging import MESSAGE_TYPE_ROUTE_STATUS_UPDATED
from agentic_mesh.messaging import MESSAGE_TYPE_SDLC_HANDOFF
from agentic_mesh.messaging import MESSAGE_TYPE_SPONSOR_DIRECTIVE_ACKNOWLEDGED
from agentic_mesh.messaging import MESSAGE_TYPE_SPONSOR_DIRECTIVE_COMPLETED
from agentic_mesh.messaging import MESSAGE_TYPE_SPONSOR_DIRECTIVE_PUBLISH_READY
from agentic_mesh.messaging import MESSAGE_TYPE_SPONSOR_DIRECTIVE_REQUESTED
from agentic_mesh.messaging import MESSAGE_TYPE_SPONSOR_DIRECTIVE_STARTED
from agentic_mesh.models import ConnectorMessage
from agentic_mesh.models import Message
from agentic_mesh.models import ProjectConnectorConfig
from agentic_mesh.models import ProjectConfig
from agentic_mesh.models import new_id
from agentic_mesh.models import utc_now_iso
from agentic_mesh.gateway import GatewayService
from agentic_mesh.gateway import GatewayStore
from agentic_mesh.gateway import event_from_message
from agentic_mesh.notification_display import build_notification_display_facts
from agentic_mesh.notifications import MESSAGE_TYPE_NOTIFICATION_EVENT
from agentic_mesh.storage import FileMessageStore
from agentic_mesh.storage import FileConnectorOutbox
from agentic_mesh.threaded_context import BindingResult
from agentic_mesh.threaded_context import FileThreadedContextStore
from agentic_mesh.threaded_context import MESSAGE_TYPE_THREADED_CONTEXT_ATTENTION_REQUESTED
from agentic_mesh.threaded_context import ThreadRouteRecord
from agentic_mesh.threaded_context import has_explicit_linked_new_work_intent
from agentic_mesh.work_queue import FileWorkQueueStore
from agentic_mesh.work_queue import SourceAnchor


class ConnectorAdapter:
    def process_once(self, channel: str) -> bool:
        raise NotImplementedError


class LocalTeamsConnectorAdapter(ConnectorAdapter):
    """Local stand-in for a future Microsoft Teams Graph connector."""

    def __init__(
        self,
        *,
        connector_id: str,
        project_id: str,
        state_root: Path,
        outbox: FileConnectorOutbox,
        journal: EventJournal,
    ) -> None:
        self.connector_id = connector_id
        self.project_id = project_id
        self.state_root = state_root
        self.outbox = outbox
        self.journal = journal

    def process_once(self, channel: str) -> bool:
        message = self.outbox.claim_next(channel, self.connector_id)
        if message is None:
            return False

        attrs = telemetry.span_attributes(
            project_id=self.project_id,
            connector_id=self.connector_id,
            channel=message.channel,
            message_id=message.message_id,
            message_type=message.type,
            work_item_id=message.payload.get("work_item_id"),
            lifecycle_state=message.payload.get("lifecycle_state"),
            gate_id=message.payload.get("gate_id"),
            correlation_id=message.correlation_id,
        )
        with telemetry.start_span(
            "connector.process",
            correlation_id=message.correlation_id,
            trace_context=message.trace_context,
            attributes=attrs,
        ):
            rendered = self._render_message(message)
            path = self._sent_path(message)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8") as handle:
                json.dump(rendered, handle, indent=2, sort_keys=True)
                handle.write("\n")

            self.journal.append(
                "teams_connector_message_prepared",
                project_id=self.project_id,
                connector_id=self.connector_id,
                channel=message.channel,
                message_id=message.message_id,
                message_type=message.type,
                work_item_id=message.payload.get("work_item_id"),
                lifecycle_state=message.payload.get("lifecycle_state"),
                gate_id=message.payload.get("gate_id"),
                correlation_id=message.correlation_id,
                rendered_path=str(path),
            )
            self.outbox.complete(message, "prepared")
        return True

    def _sent_path(self, message: ConnectorMessage) -> Path:
        timestamp = message.created_at.replace(":", "")
        return (
            self.state_root
            / "projects"
            / self.project_id
            / "connectors"
            / "teams"
            / message.channel
            / "sent"
            / f"{timestamp}-{message.message_id}.json"
        )

    def _render_message(self, message: ConnectorMessage) -> dict[str, Any]:
        rendered: dict[str, Any] = {
            "connector": "microsoft-teams-local",
            "connector_id": self.connector_id,
            "channel": message.channel,
            "message_id": message.message_id,
            "message_type": message.type,
            "correlation_id": message.correlation_id,
            "source": message.source,
            "payload": message.payload,
        }
        if message.type == MESSAGE_TYPE_HUMAN_RESPONSE_REQUESTED:
            rendered["adaptive_card"] = build_human_response_card(message)
        if message.type == MESSAGE_TYPE_SDLC_HANDOFF:
            rendered["teams_message"] = render_sdlc_handoff_html(message)
        if message.type in {
            MESSAGE_TYPE_SPONSOR_DIRECTIVE_STARTED,
            MESSAGE_TYPE_SPONSOR_DIRECTIVE_COMPLETED,
        }:
            rendered["teams_message"] = render_sponsor_directive_status_html(message)
        if message.type == MESSAGE_TYPE_SPONSOR_DIRECTIVE_PUBLISH_READY:
            rendered["teams_message"] = render_sponsor_directive_publish_ready_html(message)
        if message.type == MESSAGE_TYPE_PROBLEM_STATUS_UPDATED:
            rendered["teams_message"] = render_problem_status_html(message)
        if message.type == MESSAGE_TYPE_ROUTE_STATUS_UPDATED:
            rendered["teams_message"] = render_route_status_html(message)
        if message.type == MESSAGE_TYPE_NOTIFICATION_EVENT:
            rendered["teams_message"] = render_notification_event_html(message)
        if message.type == "threaded_context.receipt":
            rendered["teams_message"] = render_threaded_context_receipt_html(message)
        if message.type == "gateway.receipt":
            rendered["teams_message"] = render_gateway_receipt_html(message)
        return rendered

    def _human_response_card(self, message: ConnectorMessage) -> dict[str, Any]:
        return build_human_response_card(message)

    @staticmethod
    def _human_response_submit_data(
        payload: dict[str, Any],
        *,
        response_value: Any | None = None,
    ) -> dict[str, Any]:
        data = {
            "action": "human_response.submit",
            "project_id": payload.get("project_id"),
            "role_id": payload.get("role_id"),
            "work_item_id": payload.get("work_item_id"),
            "work_item_type": payload.get("work_item_type"),
            "lifecycle_state": payload.get("lifecycle_state"),
            "approval_request_id": payload.get("approval_request_id"),
            "response_request_id": payload.get("response_request_id"),
            "notification_attempt_id": payload.get("notification_attempt_id"),
            "gate_id": payload.get("gate_id"),
            "response_type": payload.get("response_type"),
            "correlation_id": payload.get("correlation_id"),
        }
        if response_value is not None:
            data["response_value"] = response_value
        return data

    @staticmethod
    def _input_for_template(response_template: dict[str, Any]) -> dict[str, Any]:
        input_mode = response_template.get("input_mode")
        input_id = "response_value"
        if input_mode == "number":
            return {"type": "Input.Number", "id": input_id}
        if input_mode == "multiline_text":
            return {"type": "Input.Text", "id": input_id, "isMultiline": True}
        return {"type": "Input.Text", "id": input_id}


class GraphTeamsConnectorAdapter(ConnectorAdapter):
    """Posts connector outbox messages to Microsoft Teams through Graph."""

    def __init__(
        self,
        *,
        connector_id: str,
        project_id: str,
        connector_config: ProjectConnectorConfig,
        outbox: FileConnectorOutbox,
        journal: EventJournal,
        token: str,
    ) -> None:
        self.connector_id = connector_id
        self.project_id = project_id
        self.connector_config = connector_config
        self.outbox = outbox
        self.journal = journal
        self.token = token

    def process_once(self, channel: str) -> bool:
        message = self.outbox.claim_next(channel, self.connector_id)
        if message is None:
            return False

        attrs = telemetry.span_attributes(
            project_id=self.project_id,
            connector_id=self.connector_id,
            channel=message.channel,
            message_id=message.message_id,
            message_type=message.type,
            work_item_id=message.payload.get("work_item_id"),
            lifecycle_state=message.payload.get("target_lifecycle_state")
            or message.payload.get("lifecycle_state"),
            correlation_id=message.correlation_id,
        )
        with telemetry.start_span(
            "connector.process",
            correlation_id=message.correlation_id,
            trace_context=message.trace_context,
            attributes=attrs,
        ):
            try:
                with telemetry.start_span(
                    "teams.send",
                    correlation_id=message.correlation_id,
                    trace_context=message.trace_context,
                    attributes=attrs,
                ):
                    send_started = time.perf_counter()
                    response = self._post_message(message)
                    telemetry.record_duration(
                        "agentic_mesh.teams.send.duration",
                        time.perf_counter() - send_started,
                        attrs,
                    )
            except Exception as exc:
                error_class = _connector_error_class(exc, prefix="Graph")
                if message.type == MESSAGE_TYPE_PROBLEM_STATUS_UPDATED:
                    self.journal.append(
                        "problem_status_notification_failed",
                        project_id=self.project_id,
                        connector_id=self.connector_id,
                        channel=message.channel,
                        message_id=message.message_id,
                        message_type=message.type,
                        work_item_id=message.payload.get("work_item_id"),
                        lifecycle_state=message.payload.get("lifecycle_state"),
                        correlation_id=message.correlation_id,
                        notification_result="failed",
                        notification_error_class=error_class,
                    )
                self.journal.append(
                    "teams_graph_message_failed",
                    project_id=self.project_id,
                    connector_id=self.connector_id,
                    channel=message.channel,
                    message_id=message.message_id,
                    message_type=message.type,
                    work_item_id=message.payload.get("work_item_id"),
                    lifecycle_state=message.payload.get("target_lifecycle_state")
                    or message.payload.get("lifecycle_state"),
                    correlation_id=message.correlation_id,
                    error=error_class,
                    redacted_error_class=error_class,
                )
                self.outbox.complete(message, "failed")
                return True

            self.journal.append(
                "teams_graph_message_sent",
                project_id=self.project_id,
                connector_id=self.connector_id,
                channel=message.channel,
                message_id=message.message_id,
                message_type=message.type,
                work_item_id=message.payload.get("work_item_id"),
                lifecycle_state=message.payload.get("target_lifecycle_state")
                or message.payload.get("lifecycle_state"),
                correlation_id=message.correlation_id,
                teams_message_id=response.get("id"),
                teams_web_url=response.get("webUrl"),
            )
            self.outbox.complete(message, "sent")
        return True

    def _post_message(self, message: ConnectorMessage) -> dict[str, Any]:
        channel_config = self.connector_config.channels[message.channel]
        url = (
            "https://graph.microsoft.com/v1.0/teams/"
            f"{quote(self.connector_config.team_id, safe='')}/channels/"
            f"{quote(channel_config.channel_id, safe='')}/messages"
        )
        payload = {
            "body": {
                "contentType": "html",
                "content": self._render_html(message),
            }
        }
        body = json.dumps(payload).encode("utf-8")
        req = request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with request.urlopen(req, timeout=30) as response:
                response_body = response.read().decode("utf-8")
        except HTTPError as exc:
            raise RuntimeError(f"GraphSendFailed{exc.code}") from exc
        return json.loads(response_body) if response_body else {}

    @staticmethod
    def _render_html(message: ConnectorMessage) -> str:
        if message.type == MESSAGE_TYPE_SDLC_HANDOFF:
            return render_sdlc_handoff_html(message)
        if message.type == MESSAGE_TYPE_HUMAN_RESPONSE_REQUESTED:
            return render_human_response_request_html(message)
        if message.type == MESSAGE_TYPE_SPONSOR_DIRECTIVE_ACKNOWLEDGED:
            return render_sponsor_directive_acknowledgement_html(message)
        if message.type in {
            MESSAGE_TYPE_SPONSOR_DIRECTIVE_STARTED,
            MESSAGE_TYPE_SPONSOR_DIRECTIVE_COMPLETED,
        }:
            return render_sponsor_directive_status_html(message)
        if message.type == MESSAGE_TYPE_PROBLEM_STATUS_UPDATED:
            return render_problem_status_html(message)
        if message.type == MESSAGE_TYPE_ROUTE_STATUS_UPDATED:
            return render_route_status_html(message)
        if message.type == MESSAGE_TYPE_NOTIFICATION_EVENT:
            return render_notification_event_html(message)
        if message.type == "threaded_context.receipt":
            return render_threaded_context_receipt_html(message)
        return (
            "<p><strong>Agentic Mesh message</strong></p>"
            f"<pre>{html.escape(json.dumps(message.payload, indent=2))}</pre>"
        )


def load_graph_token(
    token: str | None,
    token_file: Path | None,
    *,
    tenant_id: str | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
    client_secret_file: Path | None = None,
) -> str:
    if token:
        return token
    if token_file and token_file.exists():
        return token_file.read_text(encoding="utf-8").strip()
    env_token = os.getenv("AGENTIC_MESH_GRAPH_TOKEN")
    if env_token:
        return env_token
    env_token_file = os.getenv("AGENTIC_MESH_GRAPH_TOKEN_FILE")
    if env_token_file:
        return Path(env_token_file).read_text(encoding="utf-8").strip()
    tenant_id = tenant_id or os.getenv("AGENTIC_MESH_GRAPH_TENANT_ID")
    client_id = client_id or os.getenv("AGENTIC_MESH_GRAPH_CLIENT_ID")
    client_secret = client_secret or os.getenv("AGENTIC_MESH_GRAPH_CLIENT_SECRET")
    env_secret_file = os.getenv("AGENTIC_MESH_GRAPH_CLIENT_SECRET_FILE")
    client_secret_file = client_secret_file or (
        Path(env_secret_file) if env_secret_file else None
    )
    if client_secret is None and client_secret_file and client_secret_file.exists():
        client_secret = client_secret_file.read_text(encoding="utf-8").strip()
    if tenant_id and client_id and client_secret:
        return graph_client_credentials_token(
            tenant_id=tenant_id,
            client_id=client_id,
            client_secret=client_secret,
        )
    raise ValueError(
        "Graph Teams connector requires a token, token file, or client credentials"
    )


def _connector_error_class(error: BaseException, *, prefix: str) -> str:
    text = str(error)
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", text).strip("_")
    if safe.startswith(prefix) and len(safe) <= 80:
        return safe
    return f"{prefix}{error.__class__.__name__}"


def graph_client_credentials_token(
    *,
    tenant_id: str,
    client_id: str,
    client_secret: str,
) -> str:
    token_url = (
        "https://login.microsoftonline.com/"
        f"{quote(tenant_id, safe='')}/oauth2/v2.0/token"
    )
    data = urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
            "scope": "https://graph.microsoft.com/.default",
        }
    ).encode("utf-8")
    req = request.Request(
        token_url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with request.urlopen(req, timeout=30) as response:
            token_response = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(f"GraphTokenRequestFailed{exc.code}") from exc
    return str(token_response["access_token"])


class FileSecretResolver:
    def __init__(self, secret_root: Path) -> None:
        self.secret_root = secret_root

    def get(self, secret_ref: str) -> str:
        path = self.secret_root / secret_ref
        return path.read_text(encoding="utf-8").strip()


def bot_framework_token(
    *,
    tenant_id: str | None,
    app_id: str,
    app_secret: str,
) -> str:
    if not tenant_id:
        raise ValueError("Bot Framework connector requires tenant_id")
    token_url = (
        "https://login.microsoftonline.com/"
        f"{tenant_id}/oauth2/v2.0/token"
    )
    data = urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": app_id,
            "client_secret": app_secret,
            "scope": "https://api.botframework.com/.default",
        }
    ).encode("utf-8")
    req = request.Request(
        token_url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with request.urlopen(req, timeout=30) as response:
            token_response = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(f"BotTokenRequestFailed{exc.code}") from exc
    return str(token_response["access_token"])


class BotFrameworkTeamsConnectorAdapter(ConnectorAdapter):
    """Posts connector messages to Teams as the configured role bot."""

    def __init__(
        self,
        *,
        connector_id: str,
        project_id: str,
        connector_config: ProjectConnectorConfig,
        outbox: FileConnectorOutbox,
        journal: EventJournal,
        secrets: FileSecretResolver,
        service_url: str = "https://smba.trafficmanager.net/teams",
    ) -> None:
        self.connector_id = connector_id
        self.project_id = project_id
        self.connector_config = connector_config
        self.outbox = outbox
        self.journal = journal
        self.secrets = secrets
        self.service_url = service_url.rstrip("/")

    def process_once(self, channel: str) -> bool:
        message = self.outbox.claim_next(channel, self.connector_id)
        if message is None:
            return False

        role_id = self._sender_role(message)
        attrs = telemetry.span_attributes(
            project_id=self.project_id,
            connector_id=self.connector_id,
            role_id=role_id,
            channel=message.channel,
            message_id=message.message_id,
            message_type=message.type,
            work_item_id=message.payload.get("work_item_id"),
            lifecycle_state=message.payload.get("target_lifecycle_state")
            or message.payload.get("lifecycle_state"),
            correlation_id=message.correlation_id,
        )
        with telemetry.start_span(
            "connector.process",
            correlation_id=message.correlation_id,
            trace_context=message.trace_context,
            attributes=attrs,
        ):
            try:
                with telemetry.start_span(
                    "teams.send",
                    correlation_id=message.correlation_id,
                    trace_context=message.trace_context,
                    attributes=attrs,
                ):
                    send_started = time.perf_counter()
                    response = self._post_message(message, role_id)
                    telemetry.record_duration(
                        "agentic_mesh.teams.send.duration",
                        time.perf_counter() - send_started,
                        attrs,
                    )
            except Exception as exc:
                error_class = _connector_error_class(exc, prefix="Bot")
                if message.type == MESSAGE_TYPE_PROBLEM_STATUS_UPDATED:
                    self.journal.append(
                        "problem_status_notification_failed",
                        project_id=self.project_id,
                        connector_id=self.connector_id,
                        role_id=role_id,
                        channel=message.channel,
                        message_id=message.message_id,
                        message_type=message.type,
                        work_item_id=message.payload.get("work_item_id"),
                        lifecycle_state=message.payload.get("lifecycle_state"),
                        correlation_id=message.correlation_id,
                        notification_result="failed",
                        notification_error_class=error_class,
                    )
                self.journal.append(
                    "teams_bot_message_failed",
                    project_id=self.project_id,
                    connector_id=self.connector_id,
                    role_id=role_id,
                    channel=message.channel,
                    message_id=message.message_id,
                    message_type=message.type,
                    work_item_id=message.payload.get("work_item_id"),
                    lifecycle_state=message.payload.get("target_lifecycle_state")
                    or message.payload.get("lifecycle_state"),
                    correlation_id=message.correlation_id,
                    error=error_class,
                    redacted_error_class=error_class,
                )
                self.outbox.complete(message, "failed")
                return True

            self.journal.append(
                "teams_bot_message_sent",
                project_id=self.project_id,
                connector_id=self.connector_id,
                role_id=role_id,
                channel=message.channel,
                message_id=message.message_id,
                message_type=message.type,
                work_item_id=message.payload.get("work_item_id"),
                lifecycle_state=message.payload.get("target_lifecycle_state")
                or message.payload.get("lifecycle_state"),
                correlation_id=message.correlation_id,
                teams_activity_id=response.get("activityId"),
                teams_conversation_id=response.get("id"),
            )
            self.outbox.complete(message, "sent")
        return True

    def _sender_role(self, message: ConnectorMessage) -> str:
        role_id = (
            message.payload.get("source_role")
            or message.payload.get("role_id")
            or message.payload.get("target_role")
        )
        if not role_id:
            raise ValueError(f"Cannot determine sender role for {message.message_id}")
        return str(role_id)

    def _post_message(
        self,
        message: ConnectorMessage,
        role_id: str,
    ) -> dict[str, Any]:
        role_bot = self.connector_config.role_bots[role_id]
        app_id = self.secrets.get(role_bot.bot_id_ref)
        app_secret = self.secrets.get(role_bot.secret_ref)
        token = self._bot_token(app_id, app_secret)
        thread_url = self._thread_reply_url(message)
        if thread_url:
            url = thread_url
            body = self._build_thread_reply_activity(
                message,
                role_id=role_id,
                app_id=app_id,
                display_name=role_bot.display_name,
            )
        else:
            channel_config = self.connector_config.channels[message.channel]
            url = f"{self.service_url}/v3/conversations"
            body = {
                "bot": {"id": app_id, "name": role_bot.display_name},
                "isGroup": True,
                "channelData": {
                    "tenant": {"id": self.connector_config.tenant_id},
                    "team": {"id": self.connector_config.team_id},
                    "channel": {"id": channel_config.channel_id},
                },
                "activity": self._build_activity(message),
            }
        req = request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with request.urlopen(req, timeout=30) as response:
                response_body = response.read().decode("utf-8")
        except HTTPError as exc:
            raise RuntimeError(f"BotSendFailed{exc.code}") from exc
        return json.loads(response_body) if response_body else {}

    def _thread_reply_url(self, message: ConnectorMessage) -> str | None:
        payload = message.payload
        service_url = str(payload.get("teams_service_url") or "").strip()
        conversation_id = str(payload.get("teams_conversation_id") or "").strip()
        reply_to_id = str(
            payload.get("teams_reply_to_activity_id")
            or payload.get("teams_activity_id")
            or ""
        ).strip()
        if not service_url or service_url.startswith("graph://"):
            return None
        if not conversation_id or not reply_to_id:
            return None
        return (
            f"{service_url.rstrip('/')}/v3/conversations/"
            f"{quote(conversation_id, safe='')}/activities/"
            f"{quote(reply_to_id, safe='')}"
        )

    def _bot_token(self, app_id: str, app_secret: str) -> str:
        return bot_framework_token(
            tenant_id=self.connector_config.tenant_id,
            app_id=app_id,
            app_secret=app_secret,
        )

    def _build_activity(self, message: ConnectorMessage) -> dict[str, Any]:
        if message.type == MESSAGE_TYPE_HUMAN_RESPONSE_REQUESTED:
            return {
                "type": "message",
                "attachments": [
                    {
                        "contentType": "application/vnd.microsoft.card.adaptive",
                        "content": build_human_response_card(message),
                    }
                ],
            }
        activity: dict[str, Any] = {
            "type": "message",
            "textFormat": "xml",
            "text": self._render_text(message),
        }
        return activity

    def _build_thread_reply_activity(
        self,
        message: ConnectorMessage,
        *,
        role_id: str,
        app_id: str,
        display_name: str,
    ) -> dict[str, Any]:
        activity = self._build_activity(message)
        payload = message.payload
        channel_config = self.connector_config.channels[message.channel]
        conversation_id = str(payload.get("teams_conversation_id") or "")
        reply_to_id = str(
            payload.get("teams_reply_to_activity_id")
            or payload.get("teams_activity_id")
            or ""
        )
        activity["from"] = {"id": app_id, "name": display_name, "role": "bot"}
        activity["conversation"] = {"id": conversation_id}
        activity["replyToId"] = reply_to_id
        activity["channelData"] = {
            "tenant": {"id": self.connector_config.tenant_id},
            "team": {"id": self.connector_config.team_id},
            "channel": {"id": channel_config.channel_id},
            "agenticMesh": {"senderRole": role_id},
        }
        return activity

    @staticmethod
    def _render_text(message: ConnectorMessage) -> str:
        if message.type == MESSAGE_TYPE_SDLC_HANDOFF:
            payload = message.payload
            title = html.escape(str(payload.get("title") or "SDLC handoff"))
            raw_work_item_id = str(payload.get("work_item_id") or "unknown")
            work_item_id = html.escape(raw_work_item_id)
            source = html.escape(str(payload.get("source_role") or "unknown"))
            target = html.escape(str(payload.get("target_role") or "unknown"))
            source_state = html.escape(str(payload.get("source_lifecycle_state") or "unknown"))
            target_state = html.escape(str(payload.get("target_lifecycle_state") or "unknown"))
            summary = html.escape(str(payload.get("summary") or ""))
            status_link = _work_item_status_link_html(raw_work_item_id)
            return (
                f"<b>Agentic Mesh SDLC handoff: {title}</b><br/>"
                f"Work item <code>{work_item_id}</code> moved from "
                f"<code>{source_state}</code> ({source}) to "
                f"<code>{target_state}</code> ({target}).<br/>"
                f"{summary}{status_link}"
            )
        if message.type == MESSAGE_TYPE_HUMAN_RESPONSE_REQUESTED:
            payload = message.payload
            prompt = html.escape(str(payload.get("prompt") or "Human response requested"))
            work_item_id = html.escape(str(payload.get("work_item_id") or "unknown"))
            gate_id = html.escape(str(payload.get("gate_id") or "unknown"))
            response_type = html.escape(str(payload.get("response_type") or "unknown"))
            summary = html.escape(str(payload.get("summary") or ""))
            return (
                f"<b>{prompt}</b><br/>"
                f"Work item <code>{work_item_id}</code> is waiting at gate "
                f"<code>{gate_id}</code> for response type "
                f"<code>{response_type}</code>.<br/>{summary}"
            )
        if message.type == MESSAGE_TYPE_SPONSOR_DIRECTIVE_ACKNOWLEDGED:
            return _html_to_teams_xml_text(render_sponsor_directive_acknowledgement_html(message))
        if message.type in {
            MESSAGE_TYPE_SPONSOR_DIRECTIVE_STARTED,
            MESSAGE_TYPE_SPONSOR_DIRECTIVE_COMPLETED,
        }:
            return _html_to_teams_xml_text(render_sponsor_directive_status_html(message))
        if message.type == MESSAGE_TYPE_SPONSOR_DIRECTIVE_PUBLISH_READY:
            return _html_to_teams_xml_text(render_sponsor_directive_publish_ready_html(message))
        if message.type == MESSAGE_TYPE_PROBLEM_STATUS_UPDATED:
            return _html_to_teams_xml_text(render_problem_status_html(message))
        if message.type == MESSAGE_TYPE_ROUTE_STATUS_UPDATED:
            return _html_to_teams_xml_text(render_route_status_html(message))
        if message.type == MESSAGE_TYPE_NOTIFICATION_EVENT:
            return _html_to_teams_xml_text(render_notification_event_html(message))
        if message.type == "threaded_context.receipt":
            return _html_to_teams_xml_text(render_threaded_context_receipt_html(message))
        if message.type == "gateway.receipt":
            return _html_to_teams_xml_text(render_gateway_receipt_html(message))
        return html.escape(json.dumps(message.payload, indent=2))


def render_threaded_context_receipt_html(message: ConnectorMessage) -> str:
    payload = message.payload
    work_item_id = html.escape(str(payload.get("work_item_id") or "unknown"))
    context_id = html.escape(str(payload.get("threaded_context_id") or "unknown"))
    action_state = html.escape(str(payload.get("action_state") or "captured"))
    attention_state = html.escape(str(payload.get("attention_state") or "not_required"))
    return (
        "<p><strong>Agentic Mesh captured this threaded reply.</strong></p>"
        f"<p>Parent work item <code>{work_item_id}</code>; context "
        f"<code>{context_id}</code>; state <code>{action_state}</code>; "
        f"owner attention <code>{attention_state}</code>.</p>"
    )


def render_gateway_receipt_html(message: ConnectorMessage) -> str:
    payload = message.payload
    receipt = payload.get("receipt") if isinstance(payload.get("receipt"), dict) else {}
    label = html.escape(str(receipt.get("label") or "Agentic Mesh gateway received this."))
    outcome = html.escape(str(payload.get("outcome") or receipt.get("outcome") or "unknown"))
    queue_item_id = payload.get("queue_item_id") or receipt.get("queue_item_id")
    owner_role = payload.get("owner_role") or receipt.get("owner_role")
    next_action = html.escape(str(receipt.get("next_action") or ""))
    parts = [
        f"<p><strong>{label}</strong></p>",
        f"<p>Outcome <code>{outcome}</code>.</p>",
    ]
    if queue_item_id:
        parts.append(f"<p>Queue item <code>{html.escape(str(queue_item_id))}</code>.</p>")
    if owner_role:
        parts.append(f"<p>Owner <code>{html.escape(str(owner_role))}</code>.</p>")
    if next_action:
        parts.append(f"<p>{next_action}</p>")
    return "".join(parts)


def render_notification_event_html(message: ConnectorMessage) -> str:
    attrs = telemetry.span_attributes(
        project_id=message.payload.get("project_id"),
        event_kind=message.payload.get("event_kind"),
        visibility=message.payload.get("visibility"),
        work_item_id=message.payload.get("work_item_id"),
        work_item_type=message.payload.get("work_item_type"),
        lifecycle_state=message.payload.get("lifecycle_state"),
        queue_item_id=message.payload.get("queue_item_id"),
        owner_role=message.payload.get("owner_role"),
        affected_role=message.payload.get("affected_role"),
        correlation_id=message.correlation_id,
    )
    with telemetry.start_span(
        "notification.display_facts_built",
        correlation_id=message.correlation_id,
        trace_context=message.trace_context,
        attributes=attrs,
    ):
        facts = build_notification_display_facts(message.payload)
    render_attrs = telemetry.span_attributes(
        **attrs,
        display_category=facts.get("display_category"),
    )
    with telemetry.start_span(
        "notification.message_rendered",
        correlation_id=message.correlation_id,
        trace_context=message.trace_context,
        attributes=render_attrs,
    ):
        return _render_notification_display_facts_html(facts, message)


def _render_notification_display_facts_html(
    facts: dict[str, Any],
    message: ConnectorMessage,
) -> str:
    label = html.escape(str(facts["display_label"]))
    title = html.escape(str(facts["title"]))
    summary = html.escape(str(facts.get("summary") or ""))
    fact_lines = []
    for item in facts.get("facts") or []:
        label_text = html.escape(str(item.get("label") or "Fact"))
        value_text = html.escape(str(item.get("value") or ""))
        if item.get("code"):
            value_text = f"<code>{value_text}</code>"
        fact_lines.append(f"<strong>{label_text}:</strong> {value_text}")
    fact_html = f"<p>{'<br/>'.join(fact_lines)}</p>" if fact_lines else ""

    detail_items = [
        html.escape(str(item))
        for item in facts.get("detail_items") or []
        if item
    ]
    detail_html = ""
    if detail_items:
        detail_html = "<ul>" + "".join(f"<li>{item}</li>" for item in detail_items) + "</ul>"

    action_html = ""
    if facts.get("action_needed"):
        action_lines = [
            f"<strong>Action owner:</strong> {html.escape(str(facts.get('action_owner') or 'Action owner unknown'))}",
            f"<strong>Next action:</strong> {html.escape(str(facts.get('next_action') or 'Review work item status for next action'))}",
        ]
        if facts.get("retryability_label"):
            action_lines.append(
                f"<strong>Retryable:</strong> {html.escape(str(facts['retryability_label']))}"
            )
        action_html = f"<p>{'<br/>'.join(action_lines)}</p>"

    status_links = _render_links(facts.get("status_links") or [])
    artifact_links = _render_links(facts.get("artifact_links") or [])
    link_parts = []
    if status_links:
        link_parts.append(status_links)
    if artifact_links:
        link_parts.append(artifact_links)
    link_html = f"<p>{' | '.join(link_parts)}</p>" if link_parts else ""

    notices = []
    if facts.get("truncation_notice"):
        notices.append(html.escape(str(facts["truncation_notice"])))
    overflow = facts.get("artifact_overflow")
    if isinstance(overflow, dict) and overflow.get("notice"):
        notice = html.escape(str(overflow["notice"]))
        if notice not in notices:
            notices.append(notice)
    notice_html = f"<p>{' '.join(notices)}</p>" if notices else ""

    footer_lines = []
    if facts.get("occurred_at") or message.created_at:
        footer_lines.append(
            f"<strong>Occurred:</strong> {html.escape(str(facts.get('occurred_at') or message.created_at))}"
        )
    source = facts.get("source") if isinstance(facts.get("source"), dict) else {}
    if source.get("source_anchor_ref"):
        footer_lines.append(
            f"<strong>Source:</strong> <code>{html.escape(str(source['source_anchor_ref']))}</code>"
        )
    if facts.get("correlation_id"):
        footer_lines.append(
            f"<strong>Correlation:</strong> <code>{html.escape(str(facts['correlation_id']))}</code>"
        )
    footer_html = f"<p>{'<br/>'.join(footer_lines)}</p>" if footer_lines else ""
    return (
        f"<p><strong>{label}: {title}</strong></p>"
        f"<p>{summary}</p>"
        f"{fact_html}"
        f"{detail_html}"
        f"{action_html}"
        f"{link_html}"
        f"{notice_html}"
        f"{footer_html}"
    )


def _render_links(links: list[dict[str, Any]]) -> str:
    safe_links = []
    for link in links:
        if not isinstance(link, dict) or not link.get("available") or not link.get("href"):
            continue
        href = html.escape(str(link["href"]), quote=True)
        text = html.escape(str(link.get("label") or "Open status"))
        safe_links.append(f'<a href="{href}">{text}</a>')
    return " | ".join(safe_links)


def render_sdlc_handoff_html(message: ConnectorMessage) -> str:
    payload = message.payload
    title = html.escape(str(payload.get("title") or "SDLC handoff"))
    raw_work_item_id = str(payload.get("work_item_id") or "unknown")
    work_item_id = html.escape(raw_work_item_id)
    source = html.escape(str(payload.get("source_role") or "unknown"))
    target = html.escape(str(payload.get("target_role_display_name") or payload.get("target_role") or "unknown"))
    source_state = html.escape(str(payload.get("source_lifecycle_state") or "unknown"))
    target_state = html.escape(str(payload.get("target_lifecycle_state") or "unknown"))
    summary = html.escape(str(payload.get("summary") or ""))
    status_link = _work_item_status_link_html(raw_work_item_id, paragraph=True)
    return (
        f"<p><strong>Agentic Mesh SDLC handoff: {title}</strong></p>"
        f"<p>Work item <code>{work_item_id}</code> moved from "
        f"<code>{source_state}</code> ({source}) to <code>{target_state}</code> "
        f"({target}).</p>"
        f"<p>{summary}</p>"
        f"{status_link}"
    )


def render_human_response_request_html(message: ConnectorMessage) -> str:
    payload = message.payload
    view = _approval_decision_view(payload)
    prompt = html.escape(_request_heading(view, payload))
    raw_work_item_id = view.work_item_id or "unknown"
    work_item_id = html.escape(raw_work_item_id)
    gate_id = html.escape(view.gate_id or "unknown")
    approval_request_id = html.escape(view.response_request_id or "unknown")
    response_type = html.escape(view.response_type or "unknown")
    lifecycle_state = html.escape(view.lifecycle_state or "unknown")
    decision_scope = html.escape(view.decision_scope)
    work_summary = html.escape(view.description_summary)
    artifacts = [html.escape(str(item.get("path") or item.get("label"))) for item in view.artifacts]
    artifact_text = ", ".join(f"<code>{path}</code>" for path in artifacts[: view.artifact_inline_limit])
    if view.artifact_count > view.artifact_inline_limit:
        artifact_text += f", and {view.artifact_count - view.artifact_inline_limit} more"
    artifact_text = artifact_text or "none recorded"
    status_link = _work_item_status_link_html(raw_work_item_id, paragraph=True)
    test_url = view.test_url or view.status_url
    test_link = (
        f'<p><a href="{html.escape(str(test_url))}">'
        f'{html.escape(str(view.test_label or "Review and test"))}</a></p>'
        if test_url
        else ""
    )
    return (
        f"<p><strong>{prompt}</strong></p>"
        f"<p>Work item <code>{work_item_id}</code> is waiting at gate "
        f"<code>{gate_id}</code> in <code>{lifecycle_state}</code> for response type "
        f"<code>{response_type}</code>.</p>"
        f"<p><strong>Approval request:</strong> <code>{approval_request_id}</code></p>"
        f"<p><strong>Decision requested:</strong> {decision_scope}</p>"
        f"{_response_options_html(payload)}"
        f"{_timeout_html(payload)}"
        f"<p><strong>Work performed:</strong> {work_summary}</p>"
        f"<p><strong>Artifacts:</strong> {artifact_text}</p>"
        f"{test_link}"
        f"{status_link}"
    )


def render_sponsor_directive_acknowledgement_html(message: ConnectorMessage) -> str:
    payload = message.payload
    title = html.escape(str(payload.get("title") or "Directive received"))
    raw_work_item_id = str(payload.get("work_item_id") or "unknown")
    work_item_id = html.escape(raw_work_item_id)
    if payload.get("intake_mode") == "queued_sponsor_intake":
        queue_item_id = html.escape(str(payload.get("queue_item_id") or "not assigned"))
        owner_role = html.escape(str(payload.get("owner_role") or "unknown"))
        work_item_type = html.escape(str(payload.get("work_item_type") or "slice"))
        return (
            f"<p><strong>Agentic Mesh queued: {title}</strong></p>"
            f"<p>Created lifecycle work item <code>{work_item_id}</code> "
            f"from queue item <code>{queue_item_id}</code>.</p>"
            f"<p>Initial owner: <strong>{owner_role}</strong>. "
            f"Work item type: <code>{work_item_type}</code>.</p>"
            f"<p>This will run through the configured project flow rather than "
            f"as a direct role-only instruction.</p>"
            f"{_work_item_status_link_html(raw_work_item_id, paragraph=True)}"
        )
    branch = html.escape(str(payload.get("git_branch") or "not assigned"))
    role_count = html.escape(str(payload.get("role_count") or 0))
    roles = payload.get("target_roles") or []
    role_text = ", ".join(str(role) for role in roles)
    role_text = html.escape(role_text)
    return (
        f"<p><strong>Agentic Mesh received: {title}</strong></p>"
        f"<p>Created direct work item <code>{work_item_id}</code> for "
        f"<strong>{role_count}</strong> roles. This is not a lifecycle handoff "
        f"and does not require release approval.</p>"
        f"<p>Publication branch: <code>{branch}</code></p>"
        f"<p>Roles: {role_text}</p>"
        f"{_work_item_status_link_html(raw_work_item_id, paragraph=True)}"
    )


def render_sponsor_directive_status_html(message: ConnectorMessage) -> str:
    payload = message.payload
    title = html.escape(str(payload.get("title") or "Direct instruction"))
    raw_work_item_id = str(payload.get("work_item_id") or "unknown")
    work_item_id = html.escape(raw_work_item_id)
    branch = html.escape(str(payload.get("git_branch") or "not assigned"))
    role_id = html.escape(str(payload.get("role_id") or "unknown"))
    role_instance_id = html.escape(str(payload.get("role_instance_id") or "unknown"))
    status = html.escape(str(payload.get("status") or "unknown"))
    status_message = _truncate(str(payload.get("status_message") or ""), 500)
    status_message = html.escape(status_message)
    artifacts = [
        html.escape(str(path))
        for path in payload.get("artifact_paths") or []
        if path
    ]
    artifact_text = ", ".join(f"<code>{path}</code>" for path in artifacts)
    if not artifact_text:
        artifact_text = "none"
    verb = "accepted" if str(payload.get("status")) == "started" else "updated"
    return (
        f"<p><strong>{role_id}: {status} direct instruction</strong></p>"
        f"<p><code>{role_instance_id}</code> {verb} work item "
        f"<code>{work_item_id}</code>: {title}</p>"
        f"<p>{status_message}</p>"
        f"<p>Publication branch: <code>{branch}</code></p>"
        f"<p>Artifacts: {artifact_text}</p>"
        f"{_work_item_status_link_html(raw_work_item_id, paragraph=True)}"
    )


def render_route_status_html(message: ConnectorMessage) -> str:
    payload = message.payload
    route = payload.get("route_status") or {}
    title = html.escape(str(payload.get("title") or "Route requested"))
    raw_work_item_id = str(route.get("work_item_id") or payload.get("work_item_id") or "unknown")
    work_item_id = html.escape(raw_work_item_id)
    route_status = html.escape(str(route.get("route_status") or "route_requested"))
    route_kind = html.escape(str(route.get("route_kind") or "configured_route"))
    source_role = html.escape(str(route.get("source_role") or payload.get("source_role") or "unknown"))
    target_role = html.escape(str(route.get("target_role") or payload.get("target_role") or "unknown"))
    source_state = html.escape(str(route.get("source_lifecycle_state") or "unknown"))
    target_state = html.escape(str(route.get("target_lifecycle_state") or "unknown"))
    summary = html.escape(_truncate(str(payload.get("summary") or ""), 700))
    defect_id = route.get("defect_id")
    required_change = route.get("required_change")
    evidence_required = route.get("evidence_required")
    gate_id = route.get("gate_id")
    route_bits = []
    if defect_id:
        route_bits.append(f"<strong>Defect:</strong> <code>{html.escape(str(defect_id))}</code>")
    if gate_id:
        route_bits.append(f"<strong>Gate:</strong> <code>{html.escape(str(gate_id))}</code>")
    if required_change:
        route_bits.append(
            f"<strong>Required change:</strong> {html.escape(_truncate(str(required_change), 500))}"
        )
    if evidence_required:
        route_bits.append(
            f"<strong>Evidence required:</strong> {html.escape(_truncate(str(evidence_required), 500))}"
        )
    detail = "<br/>".join(route_bits)
    if detail:
        detail = f"<p>{detail}</p>"
    fallback = (
        "<p><strong>Route note:</strong> Sent through the configured fallback route.</p>"
        if payload.get("fallback")
        else ""
    )
    status_url = route.get("status_url")
    status_link = (
        f'<p><a href="{html.escape(str(status_url))}">Open work item status</a></p>'
        if status_url
        else _work_item_status_link_html(raw_work_item_id, paragraph=True)
    )
    return (
        f"<p><strong>{route_status}: {title}</strong></p>"
        f"<p>Work item <code>{work_item_id}</code> has a "
        f"<strong>{route_kind}</strong> from <code>{source_state}</code> "
        f"({source_role}) to <code>{target_state}</code> ({target_role}).</p>"
        f"<p>{summary}</p>"
        f"{detail}"
        f"{fallback}"
        f"{status_link}"
    )


def render_problem_status_html(message: ConnectorMessage) -> str:
    payload = message.payload
    problem = payload.get("problem_status") or {}
    title = html.escape(str(payload.get("title") or "Work item problem"))
    raw_work_item_id = str(problem.get("work_item_id") or payload.get("work_item_id") or "unknown")
    work_item_id = html.escape(raw_work_item_id)
    status_label = html.escape(str(problem.get("status_label") or problem.get("status") or "Problem"))
    problem_label = html.escape(str(problem.get("problem_label") or problem.get("problem_kind") or "unknown"))
    affected_role = html.escape(str(problem.get("affected_role") or "unknown"))
    lifecycle_state = html.escape(str(problem.get("lifecycle_state") or "unknown"))
    reason = html.escape(_truncate(str(problem.get("reason_summary") or problem.get("reason") or ""), 500))
    next_action = html.escape(_truncate(str(problem.get("next_action") or ""), 350))
    action_owner = html.escape(str(problem.get("action_owner") or "unknown"))
    retryability = html.escape(str(problem.get("retryability_label") or problem.get("retryable") or "unknown"))
    fallback = (
        "<p><strong>Route note:</strong> Sent through the configured fallback route.</p>"
        if payload.get("fallback")
        else ""
    )
    status_url = problem.get("status_url")
    if isinstance(status_url, str) and status_url.startswith(("http://", "https://")):
        status_link = f'<p><a href="{html.escape(status_url)}">Open work item status</a></p>'
    else:
        status_link = _work_item_status_link_html(raw_work_item_id, paragraph=True)
    status_link = (
        status_link
        or f"<p>Work item: <code>{work_item_id}</code></p>"
    )
    artifact_paths = [
        str(path)
        for path in problem.get("artifact_paths") or []
        if path
    ][:5]
    artifact_lines = "".join(
        _artifact_link_html(path)
        for path in artifact_paths
    )
    artifact_html = (
        f"<p><strong>Evidence:</strong><br/>{artifact_lines}</p>"
        if artifact_lines
        else ""
    )
    return (
        f"<p><strong>{status_label}: {title}</strong></p>"
        f"<p><strong>Problem:</strong> {problem_label}<br/>"
        f"<strong>Work item:</strong> <code>{work_item_id}</code><br/>"
        f"<strong>Affected role:</strong> {affected_role}<br/>"
        f"<strong>Lifecycle state:</strong> <code>{lifecycle_state}</code><br/>"
        f"<strong>What happened:</strong> {reason}<br/>"
        f"<strong>Next action:</strong> {next_action}<br/>"
        f"<strong>Action owner:</strong> {action_owner}<br/>"
        f"<strong>Retryability:</strong> {retryability}</p>"
        f"{fallback}"
        f"{artifact_html}"
        f"{status_link}"
    )


def render_sponsor_directive_publish_ready_html(message: ConnectorMessage) -> str:
    payload = message.payload
    title = html.escape(str(payload.get("title") or "Direct instruction"))
    raw_work_item_id = str(payload.get("work_item_id") or "unknown")
    work_item_id = html.escape(raw_work_item_id)
    branch = html.escape(str(payload.get("git_branch") or "not assigned"))
    publication = payload.get("publication") or {}
    publication_status = str(
        payload.get("terminal_status")
        or publication.get("status")
        or "ready_to_commit_and_push"
    )
    blocked_roles = [
        html.escape(str(role))
        for role in payload.get("blocked_roles") or []
        if role
    ]
    artifacts = [
        html.escape(str(path))
        for path in payload.get("artifact_paths") or []
        if path
    ]
    artifact_text = ", ".join(f"<code>{path}</code>" for path in artifacts)
    if not artifact_text:
        artifact_text = "none"
    if publication_status == "ready_to_commit_and_push":
        heading = f"Direct work item ready to publish: {title}"
        status_text = (
            "All requested roles completed successfully for "
            f"<code>{work_item_id}</code>."
        )
    else:
        heading = f"Direct work item needs sponsor review: {title}"
        blocked_text = ", ".join(blocked_roles) if blocked_roles else "unknown"
        status_text = (
            "All requested roles reached a terminal result for "
            f"<code>{work_item_id}</code>, but at least one role did not complete "
            f"successfully. Blocked roles: {blocked_text}."
        )
    return (
        f"<p><strong>{heading}</strong></p>"
        f"<p>{status_text}</p>"
        f"<p>Publication branch: <code>{branch}</code></p>"
        f"<p>Artifacts: {artifact_text}</p>"
        f"{_work_item_status_link_html(raw_work_item_id, paragraph=True)}"
    )


def _work_item_status_link_html(
    work_item_id: str,
    *,
    paragraph: bool = False,
) -> str:
    href = _work_item_status_url(work_item_id)
    if not href:
        return ""
    link = f'<a href="{html.escape(href)}">Status</a>'
    if paragraph:
        return f"<p>{link}</p>"
    return f"<br/>{link}"


def _work_item_status_url(work_item_id: str) -> str:
    if not work_item_id or work_item_id == "unknown":
        return ""
    base_url = os.environ.get("AGENTIC_MESH_STATUS_BASE_URL")
    if not base_url:
        return ""
    parsed = urlparse(base_url)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.query
        or parsed.fragment
        or _is_loopback_status_host(parsed.hostname)
    ):
        return ""
    return f"{base_url.rstrip('/')}/work-items/{quote(work_item_id, safe='')}"


def _artifact_viewer_url(path: str) -> str:
    base_url = os.environ.get("AGENTIC_MESH_STATUS_BASE_URL")
    if not base_url:
        return ""
    parsed = urlparse(base_url)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.query
        or parsed.fragment
        or _is_loopback_status_host(parsed.hostname)
    ):
        return ""
    return f"{base_url.rstrip('/')}/artifact-viewer/{quote(path, safe='')}"


def _is_loopback_status_host(hostname: str | None) -> bool:
    if hostname is None:
        return True
    normalized = hostname.strip().lower()
    return normalized in {"localhost", "::1"} or normalized.startswith("127.")


def _artifact_link_html(path: str) -> str:
    label = html.escape(path)
    url = _artifact_viewer_url(path)
    if not url:
        return f"<code>{label}</code><br/>"
    return f'<a href="{html.escape(url)}">{label}</a><br/>'


def _html_to_teams_xml_text(value: str) -> str:
    return (
        value.replace("<p>", "")
        .replace("</p>", "<br/>")
        .replace("<strong>", "<b>")
        .replace("</strong>", "</b>")
    )


def _truncate(value: str, max_length: int) -> str:
    if len(value) <= max_length:
        return value
    return f"{value[: max_length - 3].rstrip()}..."


def _short_ref(value: str) -> str | None:
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def build_human_response_card(message: ConnectorMessage) -> dict[str, Any]:
    payload = message.payload
    response_template = payload.get("response_template") or {}
    input_mode = response_template.get("input_mode")
    view = _approval_decision_view(payload)
    artifact_text = "\n".join(
        f"- {item.get('path') or item.get('label')}"
        for item in view.artifacts[: view.artifact_inline_limit]
    )
    if view.artifact_count > view.artifact_inline_limit:
        artifact_text += f"\n- and {view.artifact_count - view.artifact_inline_limit} more"
    artifact_text = artifact_text or "none recorded"
    body = [
        {
            "type": "TextBlock",
            "text": _request_heading(view, payload),
            "weight": "Bolder",
            "wrap": True,
        },
        {
            "type": "TextBlock",
            "text": f"Work performed: {view.description_summary}",
            "wrap": True,
        },
        {
            "type": "FactSet",
            "facts": [
                {"title": "Work item", "value": view.work_item_id},
                {"title": "Lifecycle", "value": view.lifecycle_state},
                {"title": "Gate", "value": view.gate_id},
                {"title": "Request", "value": view.response_request_id},
                {"title": "Response type", "value": view.response_type or ""},
                {"title": "Decision", "value": view.decision_scope},
                {"title": "Timeout", "value": _timeout_text(payload)},
                {"title": "Context", "value": view.context_completeness},
            ],
        },
        {
            "type": "TextBlock",
            "text": f"Artifacts:\n{artifact_text}",
            "wrap": True,
        },
    ]
    card: dict[str, Any] = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.5",
        "body": body,
        "actions": [],
    }
    test_url = view.test_url or view.status_url
    if test_url:
        card["actions"].append(
            {
                "type": "Action.OpenUrl",
                "title": str(view.test_label or "Review and test"),
                "url": str(test_url),
            }
        )
    status_url = view.status_url or _work_item_status_url(view.work_item_id)
    if status_url and status_url != test_url:
        body.append(
            {
                "type": "TextBlock",
                "text": f"Status: {status_url}",
                "wrap": True,
            }
        )
    if input_mode == "choice":
        card["actions"].extend(
            [
                {
                    "type": "Action.Submit",
                    "title": str(option["label"]),
                    "msTeams": {"feedback": {"hide": True}},
                    "data": human_response_submit_data(
                        payload,
                        response_value=option["value"],
                    ),
                }
                for option in response_template.get("options", [])
            ]
        )
        return card

    card["body"].append(input_for_template(response_template))
    card["actions"].append(
        {
            "type": "Action.Submit",
            "title": "Submit",
            "msTeams": {"feedback": {"hide": True}},
            "data": human_response_submit_data(payload),
        }
    )
    return card


def _decision_scope_text(payload: dict[str, Any]) -> str:
    gate_id = str(payload.get("gate_id") or "")
    if gate_id == "release_decision_response":
        return "Final release decision. This is separate from earlier planning or implementation progression approvals."
    if gate_id == "pre_implementation_sponsor_approval":
        return "Progression into implementation for this scoped work item. Specialist review and final release approval remain separate."
    return "Human response for the configured lifecycle gate."


def _response_options_html(payload: dict[str, Any]) -> str:
    template = payload.get("response_template") or {}
    options = template.get("options") or []
    if not options:
        return ""
    labels = ", ".join(html.escape(str(option.get("label") or option.get("value"))) for option in options)
    return f"<p><strong>Options:</strong> {labels}</p>"


def _timeout_text(payload: dict[str, Any]) -> str:
    timeout = payload.get("timeout")
    on_timeout = payload.get("on_timeout")
    if not timeout and not on_timeout:
        return "not configured"
    return f"{timeout or 'not configured'}; on timeout: {on_timeout or 'not configured'}"


def _timeout_html(payload: dict[str, Any]) -> str:
    text = _timeout_text(payload)
    if text == "not configured":
        return ""
    return f"<p><strong>Timeout:</strong> {html.escape(text)}</p>"


def human_response_submit_data(
    payload: dict[str, Any],
    *,
    response_value: Any | None = None,
) -> dict[str, Any]:
    data = {
        "action": "human_response.submit",
        "project_id": payload.get("project_id"),
        "role_id": payload.get("role_id"),
        "work_item_id": payload.get("work_item_id"),
        "work_item_type": payload.get("work_item_type"),
        "lifecycle_state": payload.get("lifecycle_state"),
        "approval_request_id": payload.get("approval_request_id"),
        "response_request_id": payload.get("response_request_id"),
        "notification_attempt_id": payload.get("notification_attempt_id"),
        "gate_id": payload.get("gate_id"),
        "response_type": payload.get("response_type"),
        "correlation_id": payload.get("correlation_id"),
    }
    if response_value is not None:
        data["response_value"] = response_value
    return data


def input_for_template(response_template: dict[str, Any]) -> dict[str, Any]:
    input_mode = response_template.get("input_mode")
    input_id = "response_value"
    if input_mode == "number":
        return {"type": "Input.Number", "id": input_id}
    if input_mode == "multiline_text":
        return {"type": "Input.Text", "id": input_id, "isMultiline": True}
    return {"type": "Input.Text", "id": input_id}


def adaptive_card_invoke_response(card: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": 200,
        "type": "application/vnd.microsoft.card.adaptive",
        "value": card,
    }


def build_human_response_completed_card(
    submit_payload: dict[str, Any],
    *,
    responder: str,
    response_value: Any,
    recorded_view_model: dict[str, Any] | None = None,
) -> dict[str, Any]:
    view = (
        ApprovalDecisionViewModel.from_dict(recorded_view_model)
        if recorded_view_model
        else build_recorded_approval_decision(
            None,
            request_metadata=submit_payload,
            decision_value=response_value,
            responder_display=responder,
            recorded_at=None,
        )
    )
    response_label = view.decision_label or response_value_label(
        submit_payload,
        response_value=response_value,
    )
    facts = [
        {"title": "Work item", "value": view.work_item_id},
        {"title": "Lifecycle", "value": view.lifecycle_state},
        {"title": "Gate", "value": view.gate_id},
        {"title": "Request", "value": view.response_request_id},
        {"title": "Decision", "value": response_label},
        {"title": "Responder", "value": view.responder_display or responder},
    ]
    if view.recorded_at:
        facts.append({"title": "Recorded", "value": view.recorded_at})
    if view.context_completeness != "complete":
        facts.append({"title": "Context", "value": view.context_completeness})
    body: list[dict[str, Any]] = [
        {
            "type": "TextBlock",
            "text": "Release decision recorded",
            "weight": "Bolder",
            "wrap": True,
        },
        {
            "type": "TextBlock",
            "text": view.title,
            "weight": "Bolder",
            "wrap": True,
        },
        {
            "type": "TextBlock",
            "text": view.description_summary,
            "wrap": True,
        },
        {"type": "FactSet", "facts": facts},
    ]
    if view.artifacts:
        artifact_text = "\n".join(
            f"- {item.get('path') or item.get('label')}"
            for item in view.artifacts[: view.artifact_inline_limit]
        )
        if view.artifact_count > view.artifact_inline_limit:
            artifact_text += f"\n- and {view.artifact_count - view.artifact_inline_limit} more"
        body.append({"type": "TextBlock", "text": f"Artifacts:\n{artifact_text}", "wrap": True})
    if view.context_completeness != "complete":
        body.append(
            {
                "type": "TextBlock",
                "text": "Decision context partially available.",
                "wrap": True,
            }
        )
    return {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.5",
        "body": body,
        "actions": [
            {
                "type": "Action.Submit",
                "title": response_label,
                "isEnabled": False,
                "data": {
                    "action": "human_response.completed",
                    "work_item_id": submit_payload.get("work_item_id"),
                    "response_value": response_value,
                },
            }
        ],
    }


def build_human_response_pending_card(
    submit_payload: dict[str, Any],
    *,
    validation_reason: str,
) -> dict[str, Any]:
    return {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.5",
        "body": [
            {
                "type": "TextBlock",
                "text": "Decision received for validation",
                "weight": "Bolder",
                "wrap": True,
            },
            {
                "type": "FactSet",
                "facts": [
                    {"title": "Work item", "value": submit_payload.get("work_item_id") or ""},
                    {"title": "Lifecycle", "value": submit_payload.get("lifecycle_state") or ""},
                    {"title": "Gate", "value": submit_payload.get("gate_id") or ""},
                    {"title": "Validation", "value": validation_reason},
                ],
            },
        ],
        "actions": [],
    }


def _approval_decision_view(payload: dict[str, Any]) -> ApprovalDecisionViewModel:
    if isinstance(payload.get("approval_decision_view"), dict):
        return ApprovalDecisionViewModel.from_dict(payload["approval_decision_view"])
    return build_requested_approval_decision(payload)


def _request_heading(
    view: ApprovalDecisionViewModel,
    payload: dict[str, Any],
) -> str:
    if view.gate_id == "release_decision_response":
        return "Release approval requested"
    return str(payload.get("prompt") or "Human response requested")


def response_value_label(
    payload: dict[str, Any],
    *,
    response_value: Any,
) -> str:
    response_template = payload.get("response_template") or {}
    for option in response_template.get("options", []):
        if option.get("value") == response_value:
            return str(option.get("label") or response_value)
    if response_value == "approved":
        return "Approve"
    if response_value == "not_approved":
        return "Not Approve"
    return str(response_value or "Submitted")


class TeamsBotIngress:
    """Accepts Teams bot activities and normalizes supported actions."""

    def __init__(
        self,
        *,
        connector_id: str,
        project_id: str,
        state_root: Path,
        message_store: FileMessageStore,
        journal: EventJournal,
        connector_config: ProjectConnectorConfig | None = None,
        project_config: ProjectConfig | None = None,
        secrets: FileSecretResolver | None = None,
        connector_outbox: FileConnectorOutbox | None = None,
        work_queue: FileWorkQueueStore | None = None,
    ) -> None:
        self.connector_id = connector_id
        self.project_id = project_id
        self.state_root = state_root
        self.message_store = message_store
        self.journal = journal
        self.connector_config = connector_config
        self.project_config = project_config
        self.secrets = secrets
        self.connector_outbox = connector_outbox
        self.human_gate_store = FileHumanGateRequestStore(state_root, project_id)
        self.human_response_submission_service = HumanResponseSubmissionService(
            project_id=project_id,
            store=self.human_gate_store,
            message_store=message_store,
        )
        self.work_queue = work_queue or (
            FileWorkQueueStore(state_root, project_id, journal)
            if project_config is not None
            else None
        )
        self.threaded_contexts = (
            FileThreadedContextStore(state_root, project_id, journal)
            if project_config is not None
            else None
        )
        self.gateway_services: dict[str, GatewayService] = {}
        if project_config is not None:
            for gateway_id, gateway_config in project_config.gateways.items():
                if (
                    gateway_config.enabled
                    and gateway_config.teams is not None
                    and gateway_config.teams.connector == connector_id
                ):
                    self.gateway_services[gateway_id] = GatewayService(
                        project_id=project_id,
                        gateway_config=gateway_config,
                        store=GatewayStore(state_root, project_id),
                        journal=journal,
                        work_queue=self.work_queue,
                        role_ids=set(project_config.roles),
                    )

    def receive_activity(self, activity: dict[str, Any]) -> dict[str, Any]:
        activity_id = self._activity_id(activity)
        path = self._incoming_path(activity_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            json.dump(activity, handle, indent=2, sort_keys=True)
            handle.write("\n")

        value = activity.get("value") or {}
        correlation_id = value.get("correlation_id") if isinstance(value, dict) else None
        trace_context = value.get("trace_context") if isinstance(value, dict) else None
        attrs = telemetry.span_attributes(
            project_id=self.project_id,
            connector_id=self.connector_id,
            activity_id=activity_id,
            activity_type=activity.get("type"),
            work_item_id=value.get("work_item_id") if isinstance(value, dict) else None,
            lifecycle_state=value.get("lifecycle_state") if isinstance(value, dict) else None,
            gate_id=value.get("gate_id") if isinstance(value, dict) else None,
            correlation_id=correlation_id,
        )
        with telemetry.start_span(
            "teams.receive",
            correlation_id=correlation_id,
            trace_context=trace_context if isinstance(trace_context, dict) else None,
            attributes=attrs,
        ):
            self.journal.append(
                "teams_bot_activity_received",
                project_id=self.project_id,
                connector_id=self.connector_id,
                activity_id=activity_id,
                activity_type=activity.get("type"),
                service_url=activity.get("serviceUrl"),
                conversation_id=(activity.get("conversation") or {}).get("id"),
                from_id=(activity.get("from") or {}).get("id"),
                raw_activity_path=str(path),
            )

            if isinstance(value, dict) and value.get("action") == "human_response.submit":
                submission = self._submit_human_response(activity, value)
                if submission.final:
                    response_card = build_human_response_completed_card(
                        value,
                        responder=str(submission.responder_display or "teams-user"),
                        response_value=submission.decision_value,
                        recorded_view_model=submission.recorded_view_model,
                    )
                else:
                    response_card = build_human_response_pending_card(
                        value,
                        validation_reason=submission.validation_reason,
                    )
                self.journal.append(
                    "human_response_completion_card_returned",
                    project_id=self.project_id,
                    connector_id=self.connector_id,
                    message_id=submission.message_id,
                    work_item_id=value.get("work_item_id"),
                    lifecycle_state=value.get("lifecycle_state"),
                    gate_id=value.get("gate_id"),
                    response_request_id=submission.response_request_id,
                    response_value=submission.decision_value,
                    validation_reason=submission.validation_reason,
                    final=submission.final,
                    duplicate=submission.duplicate,
                    context_lookup=submission.context_lookup,
                    context_completeness=submission.context_completeness,
                    correlation_id=submission.correlation_id,
                )
                self._update_original_card(activity, value, response_card, submission)
                return adaptive_card_invoke_response(response_card)

            intake_messages = self._record_channel_intake(activity, path)
            if intake_messages:
                if isinstance(intake_messages, Message):
                    messages = [intake_messages]
                else:
                    messages = intake_messages
                return {
                    "status": "accepted",
                    "activity_id": activity_id,
                    "routed": True,
                    "message_id": messages[0].message_id,
                    "message_ids": [message.message_id for message in messages],
                    "target_role": messages[0].role_id,
                    "target_roles": [message.role_id for message in messages],
                    "work_item_id": messages[0].payload.get("work_item_id"),
                    "lifecycle_state": messages[0].payload.get("lifecycle_state"),
                }

        return {"status": "accepted", "activity_id": activity_id}

    def _record_channel_intake(
        self,
        activity: dict[str, Any],
        raw_activity_path: Path,
    ) -> Message | list[Message] | None:
        if activity.get("type") != "message":
            return None
        text = _plain_text(activity.get("text"))
        if not text:
            return None
        if self.project_config is None:
            self._journal_ignored_channel_message(
                activity,
                reason="project_config_not_available",
            )
            return None
        if self.connector_config is None:
            self._journal_ignored_channel_message(
                activity,
                reason="connector_config_not_available",
            )
            return None

        logical_channel = self._logical_channel_for_activity(activity)
        if logical_channel is None and self._is_gateway_dm(activity):
            self._record_gateway_intake(
                activity,
                raw_activity_path,
                logical_channel="dm",
                text=text,
                source_kind="dm",
            )
            return None
        if logical_channel is None:
            self._journal_ignored_channel_message(
                activity,
                reason="unmapped_channel",
            )
            return None
        if self._is_gateway_channel_message(activity, logical_channel):
            self._record_gateway_intake(
                activity,
                raw_activity_path,
                logical_channel=logical_channel,
                text=text,
                source_kind="channel",
            )
            return None
        threaded_result = self._record_threaded_context_if_known_parent(
            activity,
            logical_channel=logical_channel,
            text=text,
        )
        if threaded_result is not None:
            if (
                threaded_result.status == "bound"
                and threaded_result.context is not None
                and has_explicit_linked_new_work_intent(text)
            ):
                linked = self._record_sponsor_intake(
                    activity,
                    raw_activity_path,
                    logical_channel=logical_channel,
                    text=text,
                    intake_reason="linked_new_work_from_threaded_context",
                    parent_work_item_id=threaded_result.context.parent_work_item_id,
                    parent_threaded_context_id=threaded_result.context.context_id,
                )
                if linked is not None:
                    return linked
            if threaded_result.status == "bound" and threaded_result.context is not None:
                return self._route_threaded_context_attention(threaded_result)
            return None
        if logical_channel == "all-agents":
            if self._activity_mentions(activity, logical_channel):
                return self._record_all_agents_directive(
                    activity,
                    raw_activity_path,
                    logical_channel=logical_channel,
                    text=text,
                )
            targeted_roles = self._mentioned_role_ids(activity)
            if targeted_roles:
                if self._targeted_request_should_enter_queue(targeted_roles, text):
                    return self._record_sponsor_intake(
                        activity,
                        raw_activity_path,
                        logical_channel=logical_channel,
                        text=text,
                        intake_reason="targeted_delivery_slice_request",
                    )
                return self._record_targeted_directive(
                    activity,
                    raw_activity_path,
                    logical_channel=logical_channel,
                    text=text,
                    roles=targeted_roles,
                )
            self._journal_ignored_channel_message(
                activity,
                reason="missing_all_agents_or_role_mention",
            )
            return None

        return self._record_sponsor_intake(
            activity,
            raw_activity_path,
            logical_channel=logical_channel,
            text=text,
            intake_reason="channel_sponsor_intake",
        )

    def _record_threaded_context_if_known_parent(
        self,
        activity: dict[str, Any],
        *,
        logical_channel: str,
        text: str,
    ) -> BindingResult | None:
        if self.threaded_contexts is None:
            return None
        reply_to_id = activity.get("replyToId")
        graph = activity.get("graph") if isinstance(activity.get("graph"), dict) else {}
        parent_ref = (
            graph.get("parent_message_id")
            or graph.get("reply_to_id")
            or reply_to_id
        )
        if not parent_ref:
            return None

        result = self.threaded_contexts.resolve_route(
            connector_type="teams",
            connector_id=self.connector_id,
            source_scope=logical_channel,
            root_message_ref=str(parent_ref),
            candidate_connector_ids=[self.connector_id, "teams-shared"],
        )
        if result.status != "bound" or result.route is None:
            self.journal.append(
                "threaded_context.parent_not_verified",
                project_id=self.project_id,
                connector_type="teams",
                connector_id=self.connector_id,
                source_scope=logical_channel,
                reason=result.reason or "parent_not_verified",
                candidate_count=result.conflict_count,
            )
            return result

        source_anchor = self._source_anchor_for_activity(activity, logical_channel)
        source_message_ref = str(
            graph.get("message_id")
            or activity.get("id")
            or self._message_fingerprint_for_activity(activity)
        )
        capture = self.threaded_contexts.capture(
            route=result.route,
            source_message_ref=source_message_ref,
            actor_label=(activity.get("from") or {}).get("name"),
            text=text,
            mentioned_roles=self._mentioned_role_ids(activity),
            source_anchor_ref=source_anchor.source_anchor_ref(),
            correlation_id=None,
        )
        if capture.status == "bound" and capture.context is not None:
            self._queue_threaded_context_receipt(
                logical_channel=logical_channel,
                context=capture.context.to_safe_dict(),
            )
        return capture

    def _route_threaded_context_attention(
        self,
        result: BindingResult,
    ) -> Message | None:
        context = result.context
        if context is None or not context.owner_role:
            return None
        message = Message.create(
            role_id=context.owner_role,
            message_type=MESSAGE_TYPE_THREADED_CONTEXT_ATTENTION_REQUESTED,
            payload={
                "title": "Threaded context needs attention",
                "summary": context.summary,
                "work_item_id": context.parent_work_item_id,
                "work_item_type": context.parent_work_item_type,
                "lifecycle_state": context.lifecycle_state,
                "threaded_context_id": context.context_id,
                "source_anchor_ref": context.source_anchor_ref,
                "action_state": context.action_state,
                "attention_state": context.attention_state,
                "mentioned_roles": list(context.mentioned_roles),
                "context_kind": context.context_kind,
            },
            source=f"threaded-context:{context.connector_type}:{context.connector_id}",
            correlation_id=context.correlation_id,
        )
        queued = self.message_store.enqueue(message)
        self.journal.append(
            "threaded_context.owner_attention.routed",
            project_id=self.project_id,
            connector_type=context.connector_type,
            connector_id=context.connector_id,
            parent_work_item_id=context.parent_work_item_id,
            threaded_context_id=context.context_id,
            owner_role=context.owner_role,
            message_id=queued.message_id,
            correlation_id=queued.correlation_id,
        )
        return queued

    def _queue_threaded_context_receipt(
        self,
        *,
        logical_channel: str,
        context: dict[str, Any],
    ) -> None:
        if self.connector_outbox is None:
            self.journal.append(
                "threaded_context.receipt.failed",
                project_id=self.project_id,
                connector_type="teams",
                connector_id=self.connector_id,
                parent_work_item_id=context.get("parent_work_item_id"),
                threaded_context_id=context.get("context_id"),
                reason="connector_outbox_not_configured",
                correlation_id=context.get("correlation_id"),
            )
            return
        receipt = ConnectorMessage.create(
            channel=logical_channel,
            message_type="threaded_context.receipt",
            payload={
                "project_id": self.project_id,
                "title": "Threaded context captured",
                "summary": "Added this reply to the parent work item.",
                "work_item_id": context.get("parent_work_item_id"),
                "work_item_type": context.get("parent_work_item_type"),
                "lifecycle_state": context.get("lifecycle_state"),
                "threaded_context_id": context.get("context_id"),
                "action_state": context.get("action_state"),
                "attention_state": context.get("attention_state"),
                "source_anchor_ref": context.get("source_anchor_ref"),
            },
            source=f"threaded-context:{self.connector_id}:{logical_channel}",
            correlation_id=context.get("correlation_id"),
        )
        self.connector_outbox.enqueue(receipt)
        self.journal.append(
            "threaded_context.receipt.queued",
            project_id=self.project_id,
            connector_type="teams",
            connector_id=self.connector_id,
            channel=logical_channel,
            parent_work_item_id=context.get("parent_work_item_id"),
            threaded_context_id=context.get("context_id"),
            connector_message_id=receipt.message_id,
            correlation_id=context.get("correlation_id"),
        )

    def _seed_thread_route(
        self,
        activity: dict[str, Any],
        *,
        logical_channel: str,
        work_item_id: str,
        work_item_type: str,
        lifecycle_state: str | None,
        owner_role: str | None,
        source_anchor_ref: str | None,
    ) -> None:
        if self.threaded_contexts is None or not activity.get("id"):
            return
        for connector_id in {self.connector_id, "teams-shared"}:
            self.threaded_contexts.upsert_route(
                ThreadRouteRecord.create(
                    connector_type="teams",
                    connector_id=connector_id,
                    source_scope=logical_channel,
                    root_message_ref=str(activity["id"]),
                    parent_work_item_id=work_item_id,
                    parent_work_item_type=work_item_type,
                    lifecycle_state=lifecycle_state,
                    owner_role=owner_role,
                    source_anchor_ref=source_anchor_ref,
                )
            )

    def _is_gateway_dm(self, activity: dict[str, Any]) -> bool:
        if not self.gateway_services:
            return False
        conversation = activity.get("conversation") or {}
        conversation_type = str(conversation.get("conversationType") or "").casefold()
        source_scope = str(activity.get("source_scope") or "").casefold()
        return conversation_type == "personal" or source_scope == "dm"

    def _is_gateway_channel_message(
        self,
        activity: dict[str, Any],
        logical_channel: str,
    ) -> bool:
        if not self.gateway_services:
            return False
        for gateway_id, service in self.gateway_services.items():
            teams_config = service.gateway_config.teams
            if teams_config is None:
                continue
            if logical_channel not in set(teams_config.intake_channels):
                continue
            if self._activity_mentions(activity, gateway_id):
                return True
            leading_targets = {
                _normalise_mention_text(gateway_id),
                _normalise_mention_text(teams_config.bot.display_name),
            }
            text = _plain_text(activity.get("text")).strip()
            first_token = re.split(r"[:,\\s]+", text, maxsplit=1)[0] if text else ""
            if _normalise_mention_text(first_token) in leading_targets:
                return True
        return False

    def _gateway_service_for_activity(
        self,
        activity: dict[str, Any],
        logical_channel: str,
    ) -> tuple[str, GatewayService] | None:
        if not self.gateway_services:
            return None
        for gateway_id, service in self.gateway_services.items():
            teams_config = service.gateway_config.teams
            if teams_config is None:
                continue
            if logical_channel == "dm" and teams_config.dm_enabled:
                return gateway_id, service
            if logical_channel in set(teams_config.intake_channels):
                if self._activity_mentions(activity, gateway_id):
                    return gateway_id, service
                text = _plain_text(activity.get("text")).strip()
                first_token = re.split(r"[:,\\s]+", text, maxsplit=1)[0] if text else ""
                if _normalise_mention_text(first_token) in {
                    _normalise_mention_text(gateway_id),
                    _normalise_mention_text(teams_config.bot.display_name),
                }:
                    return gateway_id, service
        return None

    def _record_gateway_intake(
        self,
        activity: dict[str, Any],
        raw_activity_path: Path,
        *,
        logical_channel: str,
        text: str,
        source_kind: str,
    ) -> None:
        match = self._gateway_service_for_activity(activity, logical_channel)
        if match is None:
            self._journal_ignored_channel_message(activity, reason="gateway_not_configured")
            return
        gateway_id, service = match
        source_anchor = self._source_anchor_for_activity(activity, logical_channel)
        event = event_from_message(
            project_id=self.project_id,
            gateway_id=gateway_id,
            connector_type="teams",
            connector_id=self.connector_id,
            source_kind=source_kind,
            source_anchor=source_anchor,
            actor_label=str((activity.get("from") or {}).get("name") or "teams-user"),
            actor_source_id=(activity.get("from") or {}).get("id"),
            text=text,
            idempotency_key=self._source_idempotency_key(activity, logical_channel),
            role_hints=self._mentioned_role_ids(activity),
        )
        result = service.handle_event(event)
        self.journal.append(
            "teams_gateway_intake_processed",
            project_id=self.project_id,
            connector_id=self.connector_id,
            gateway_id=gateway_id,
            channel=logical_channel,
            gateway_event_id=event.gateway_event_id,
            gateway_result_id=result.gateway_result_id,
            outcome=result.outcome,
            queue_item_id=result.queue_item_id,
            source_anchor_ref=source_anchor.source_anchor_ref(),
            raw_activity_ref=raw_activity_path.name,
            correlation_id=result.correlation_id,
        )
        self._queue_gateway_receipt(
            logical_channel=logical_channel,
            gateway_id=gateway_id,
            result=result.to_safe_dict(),
        )

    def _queue_gateway_receipt(
        self,
        *,
        logical_channel: str,
        gateway_id: str,
        result: dict[str, Any],
    ) -> None:
        if self.connector_outbox is None:
            self.journal.append(
                "gateway.receipt.failed",
                project_id=self.project_id,
                connector_type="teams",
                connector_id=self.connector_id,
                gateway_id=gateway_id,
                reason="connector_outbox_not_configured",
                correlation_id=result.get("correlation_id"),
            )
            return
        channel = logical_channel if logical_channel != "dm" else "all-agents"
        message = ConnectorMessage.create(
            channel=channel,
            message_type="gateway.receipt",
            payload={
                "project_id": self.project_id,
                "gateway_id": gateway_id,
                "outcome": result.get("outcome"),
                "queue_item_id": result.get("queue_item_id"),
                "work_item_id": result.get("work_item_id"),
                "owner_role": result.get("owner_role"),
                "receipt": result.get("receipt") or {},
            },
            source=f"gateway:{gateway_id}",
            correlation_id=result.get("correlation_id"),
        )
        self.connector_outbox.enqueue(message)
        self.journal.append(
            "gateway.receipt.queued",
            project_id=self.project_id,
            connector_type="teams",
            connector_id=self.connector_id,
            gateway_id=gateway_id,
            channel=channel,
            connector_message_id=message.message_id,
            outcome=result.get("outcome"),
            queue_item_id=result.get("queue_item_id"),
            correlation_id=result.get("correlation_id"),
        )

    def _record_sponsor_intake(
        self,
        activity: dict[str, Any],
        raw_activity_path: Path,
        *,
        logical_channel: str,
        text: str,
        intake_reason: str,
        parent_work_item_id: str | None = None,
        parent_threaded_context_id: str | None = None,
    ) -> Message | None:
        assert self.project_config is not None
        sponsor_policy = self.project_config.flow.sponsor_initiated_work
        lifecycle_state = (
            sponsor_policy.default_intake_state
            if sponsor_policy is not None
            else self.project_config.flow.entry_state
        )
        flow_state = self.project_config.flow.states[lifecycle_state]
        work_item_type = (
            sponsor_policy.default_work_item_type
            if sponsor_policy is not None
            else (
                self.project_config.flow.work_item_types[0]
                if self.project_config.flow.work_item_types
                else "slice"
            )
        )
        work_item_type = self._recommended_sponsor_work_item_type(
            text,
            default_work_item_type=work_item_type,
        )
        work_item_id = new_id("work")
        title = _title_from_text(text)
        from_user = activity.get("from") or {}
        channel_data = activity.get("channelData") or {}
        team = channel_data.get("team") or {}
        channel = channel_data.get("channel") or {}
        conversation = activity.get("conversation") or {}
        source_anchor = self._source_anchor_for_activity(activity, logical_channel)
        idempotency_key = self._source_idempotency_key(activity, logical_channel)
        if self.work_queue is not None:
            existing = self.work_queue.get_by_idempotency_key(idempotency_key)
            if existing is not None:
                self._journal_ignored_channel_message(
                    activity,
                    reason="duplicate_source_already_queued",
                )
                return None
        queue_item = None
        if self.work_queue is not None:
            queue_item = self.work_queue.capture(
                title=title,
                summary=text,
                owner_role=flow_state.owner_role,
                source_anchor=source_anchor,
                recommended_work_item_type=work_item_type,
                idempotency_key=idempotency_key,
                metadata={
                    "work_item_id": work_item_id,
                    "work_item_type": work_item_type,
                    "intake": intake_reason,
                    **(
                        {
                            "parent_work_item_id": parent_work_item_id,
                            "parent_threaded_context_id": parent_threaded_context_id,
                        }
                        if parent_work_item_id and parent_threaded_context_id
                        else {}
                    ),
                },
                raw_payload=activity,
                retain_raw_payload=False,
            )
        payload = {
            "title": title,
            "summary": text,
            "text": text,
            "work_item_id": work_item_id,
            "work_item_type": work_item_type,
            "lifecycle_state": lifecycle_state,
            "queue_item_id": queue_item.queue_item_id if queue_item else None,
            "source_anchor": source_anchor.redacted_summary(),
            "source_connector": "teams",
            "source_connector_id": self.connector_id,
            "source_channel": logical_channel,
            "teams_activity_id": activity.get("id"),
            "teams_reply_to_activity_id": activity.get("id"),
            "teams_conversation_id": conversation.get("id"),
            "teams_service_url": activity.get("serviceUrl"),
            "teams_channel_id": channel.get("id") or conversation.get("id"),
            "teams_team_id": team.get("id"),
            "teams_from_id": from_user.get("id"),
            "teams_from_name": from_user.get("name"),
            "raw_activity_path": str(raw_activity_path),
            "parent_work_item_id": parent_work_item_id,
            "parent_threaded_context_id": parent_threaded_context_id,
        }
        message = Message.create(
            role_id=flow_state.owner_role,
            message_type="sponsor_intake.requested",
            payload=payload,
            source=f"teams:{self.connector_id}:{logical_channel}",
            correlation_id=queue_item.correlation_id if queue_item else None,
        )
        message = self.message_store.enqueue(message)
        self.journal.append(
            "teams_channel_message_routed",
            project_id=self.project_id,
            connector_id=self.connector_id,
            channel=logical_channel,
            target_role=message.role_id,
            lifecycle_state=lifecycle_state,
            work_item_id=work_item_id,
            work_item_type=work_item_type,
            message_id=message.message_id,
            queue_item_id=queue_item.queue_item_id if queue_item else None,
            source_anchor_ref=source_anchor.source_anchor_ref(),
            teams_activity_id=activity.get("id"),
            correlation_id=message.correlation_id,
        )
        self._queue_sponsor_intake_acknowledgement(
            logical_channel=logical_channel,
            title=title,
            text=text,
            work_item_id=work_item_id,
            work_item_type=work_item_type,
            owner_role=flow_state.owner_role,
            queue_item_id=queue_item.queue_item_id if queue_item else None,
            source_anchor=source_anchor.redacted_summary(),
            activity=activity,
            correlation_id=message.correlation_id,
        )
        self._seed_thread_route(
            activity,
            logical_channel=logical_channel,
            work_item_id=work_item_id,
            work_item_type=work_item_type,
            lifecycle_state=lifecycle_state,
            owner_role=flow_state.owner_role,
            source_anchor_ref=source_anchor.source_anchor_ref(),
        )
        return message

    def _record_all_agents_directive(
        self,
        activity: dict[str, Any],
        raw_activity_path: Path,
        *,
        logical_channel: str,
        text: str,
    ) -> list[Message]:
        assert self.project_config is not None
        roles = sorted(self.project_config.roles)
        return self._record_directive(
            activity,
            raw_activity_path,
            logical_channel=logical_channel,
            text=text,
            roles=roles,
            work_mode="direct_broadcast",
            routed_event="teams_all_agents_directive_routed",
            acknowledgement_event="teams_all_agents_acknowledgement_queued",
            unroutable_event="teams_all_agents_acknowledgement_unroutable",
            acknowledgement_role_id="delivery-manager",
        )

    def _record_targeted_directive(
        self,
        activity: dict[str, Any],
        raw_activity_path: Path,
        *,
        logical_channel: str,
        text: str,
        roles: list[str],
    ) -> list[Message]:
        acknowledgement_role_id = roles[0] if len(roles) == 1 else "delivery-manager"
        return self._record_directive(
            activity,
            raw_activity_path,
            logical_channel=logical_channel,
            text=text,
            roles=roles,
            work_mode="direct_targeted",
            routed_event="teams_targeted_directive_routed",
            acknowledgement_event="teams_targeted_directive_acknowledgement_queued",
            unroutable_event="teams_targeted_directive_acknowledgement_unroutable",
            acknowledgement_role_id=acknowledgement_role_id,
        )

    def _record_directive(
        self,
        activity: dict[str, Any],
        raw_activity_path: Path,
        *,
        logical_channel: str,
        text: str,
        roles: list[str],
        work_mode: str,
        routed_event: str,
        acknowledgement_event: str,
        unroutable_event: str,
        acknowledgement_role_id: str,
    ) -> list[Message]:
        assert self.project_config is not None
        assert self.connector_config is not None
        work_item_id = new_id("work")
        title = _title_from_text(text)
        git_branch = _direct_work_branch_name(work_item_id, title)
        publication = {
            "mode": "git_branch",
            "branch": git_branch,
            "status": "open",
            "commit_policy": "commit_and_push_after_all_roles_terminal",
        }
        from_user = activity.get("from") or {}
        channel_data = activity.get("channelData") or {}
        team = channel_data.get("team") or {}
        channel = channel_data.get("channel") or {}
        conversation = activity.get("conversation") or {}
        source_anchor = self._source_anchor_for_activity(activity, logical_channel)
        idempotency_key = self._source_idempotency_key(activity, logical_channel)
        if self.work_queue is not None:
            existing = self.work_queue.get_by_idempotency_key(idempotency_key)
            if existing is not None:
                self._journal_ignored_channel_message(
                    activity,
                    reason="duplicate_source_already_queued",
                )
                return []
        queue_item = None
        if self.work_queue is not None:
            queue_item = self.work_queue.capture(
                title=title,
                summary=text,
                owner_role=acknowledgement_role_id,
                source_anchor=source_anchor,
                recommended_work_item_type="spike",
                idempotency_key=idempotency_key,
                metadata={
                    "work_item_id": work_item_id,
                    "work_item_type": "directive",
                    "work_mode": work_mode,
                    "target_roles": roles,
                },
                raw_payload=activity,
                retain_raw_payload=False,
            )
        messages: list[Message] = []
        for role_id in roles:
            payload = {
                "title": title,
                "summary": text,
                "text": text,
                "work_item_id": work_item_id,
                "work_item_type": "directive",
                "work_mode": work_mode,
                "git_branch": git_branch,
                "publication": publication,
                "queue_item_id": queue_item.queue_item_id if queue_item else None,
                "source_anchor": source_anchor.redacted_summary(),
                "target_role": role_id,
                "requested_roles": roles,
                "output_path": f"documents/analysis/{role_id}.md",
                "source_connector": "teams",
                "source_connector_id": self.connector_id,
                "source_channel": logical_channel,
                "teams_activity_id": activity.get("id"),
                "teams_reply_to_activity_id": activity.get("id"),
                "teams_conversation_id": conversation.get("id"),
                "teams_service_url": activity.get("serviceUrl"),
                "teams_channel_id": channel.get("id") or conversation.get("id"),
                "teams_team_id": team.get("id"),
                "teams_from_id": from_user.get("id"),
                "teams_from_name": from_user.get("name"),
                "raw_activity_path": str(raw_activity_path),
            }
            message = Message.create(
                role_id=role_id,
                message_type=MESSAGE_TYPE_SPONSOR_DIRECTIVE_REQUESTED,
                payload=payload,
                source=f"teams:{self.connector_id}:{logical_channel}",
                correlation_id=queue_item.correlation_id if queue_item else None,
            )
            messages.append(self.message_store.enqueue(message))
        self.journal.append(
            routed_event,
            project_id=self.project_id,
            connector_id=self.connector_id,
            channel=logical_channel,
            target_roles=roles,
            role_count=len(roles),
            work_item_id=work_item_id,
            git_branch=git_branch,
            publication=publication,
            work_item_type="directive",
            queue_item_id=queue_item.queue_item_id if queue_item else None,
            source_anchor_ref=source_anchor.source_anchor_ref(),
            teams_activity_id=activity.get("id"),
            correlation_id=queue_item.correlation_id if queue_item else None,
        )
        self._queue_directive_acknowledgement(
            logical_channel=logical_channel,
            title=title,
            text=text,
            work_item_id=work_item_id,
            git_branch=git_branch,
            publication=publication,
            roles=roles,
            activity=activity,
            queue_item_id=queue_item.queue_item_id if queue_item else None,
            source_anchor=source_anchor.redacted_summary(),
            correlation_id=queue_item.correlation_id if queue_item else None,
            acknowledgement_role_id=acknowledgement_role_id,
            queued_event=acknowledgement_event,
            unroutable_event=unroutable_event,
        )
        self._seed_thread_route(
            activity,
            logical_channel=logical_channel,
            work_item_id=work_item_id,
            work_item_type="directive",
            lifecycle_state=None,
            owner_role=acknowledgement_role_id,
            source_anchor_ref=source_anchor.source_anchor_ref(),
        )
        return messages

    def _queue_sponsor_intake_acknowledgement(
        self,
        *,
        logical_channel: str,
        title: str,
        text: str,
        work_item_id: str,
        work_item_type: str,
        owner_role: str,
        queue_item_id: str | None,
        source_anchor: dict[str, Any],
        activity: dict[str, Any],
        correlation_id: str | None,
    ) -> None:
        if self.connector_outbox is None:
            self.journal.append(
                "teams_sponsor_intake_acknowledgement_unroutable",
                project_id=self.project_id,
                connector_id=self.connector_id,
                channel=logical_channel,
                work_item_id=work_item_id,
                queue_item_id=queue_item_id,
                reason="connector_outbox_not_configured",
                correlation_id=correlation_id,
            )
            return
        conversation = activity.get("conversation") or {}
        acknowledgement = ConnectorMessage.create(
            channel=logical_channel,
            message_type=MESSAGE_TYPE_SPONSOR_DIRECTIVE_ACKNOWLEDGED,
            payload={
                "project_id": self.project_id,
                "role_id": owner_role,
                "title": title,
                "summary": text,
                "work_item_id": work_item_id,
                "work_item_type": work_item_type,
                "queue_item_id": queue_item_id,
                "source_anchor": source_anchor,
                "source_channel": logical_channel,
                "owner_role": owner_role,
                "intake_mode": "queued_sponsor_intake",
                "teams_activity_id": activity.get("id"),
                "teams_reply_to_activity_id": activity.get("id"),
                "teams_conversation_id": conversation.get("id"),
                "teams_service_url": activity.get("serviceUrl"),
            },
            source=f"teams:{self.connector_id}:{logical_channel}",
            correlation_id=correlation_id,
        )
        self.connector_outbox.enqueue(acknowledgement)
        self.journal.append(
            "teams_sponsor_intake_acknowledgement_queued",
            project_id=self.project_id,
            connector_id=self.connector_id,
            channel=logical_channel,
            connector_message_id=acknowledgement.message_id,
            work_item_id=work_item_id,
            queue_item_id=queue_item_id,
            owner_role=owner_role,
            correlation_id=correlation_id,
        )

    def _queue_directive_acknowledgement(
        self,
        *,
        logical_channel: str,
        title: str,
        text: str,
        work_item_id: str,
        git_branch: str,
        publication: dict[str, Any],
        roles: list[str],
        activity: dict[str, Any],
        queue_item_id: str | None,
        source_anchor: dict[str, Any],
        correlation_id: str | None,
        acknowledgement_role_id: str,
        queued_event: str,
        unroutable_event: str,
    ) -> None:
        if self.connector_outbox is None:
            self.journal.append(
                unroutable_event,
                project_id=self.project_id,
                connector_id=self.connector_id,
                channel=logical_channel,
                work_item_id=work_item_id,
                queue_item_id=queue_item_id,
                reason="connector_outbox_not_configured",
                correlation_id=correlation_id,
            )
            return
        acknowledgement = ConnectorMessage.create(
            channel=logical_channel,
            message_type=MESSAGE_TYPE_SPONSOR_DIRECTIVE_ACKNOWLEDGED,
            payload={
                "project_id": self.project_id,
                "role_id": acknowledgement_role_id,
                "title": title,
                "summary": text,
                "work_item_id": work_item_id,
                "work_item_type": "directive",
                "queue_item_id": queue_item_id,
                "source_anchor": source_anchor,
                "git_branch": git_branch,
                "publication": publication,
                "source_channel": logical_channel,
                "target_roles": roles,
                "role_count": len(roles),
                "teams_activity_id": activity.get("id"),
                "teams_reply_to_activity_id": activity.get("id"),
                "teams_conversation_id": (activity.get("conversation") or {}).get("id"),
                "teams_service_url": activity.get("serviceUrl"),
            },
            source=f"teams:{self.connector_id}:{logical_channel}",
            correlation_id=correlation_id,
        )
        self.connector_outbox.enqueue(acknowledgement)
        self.journal.append(
            queued_event,
            project_id=self.project_id,
            connector_id=self.connector_id,
            channel=logical_channel,
            connector_message_id=acknowledgement.message_id,
            work_item_id=work_item_id,
            queue_item_id=queue_item_id,
            role_count=len(roles),
            acknowledgement_role_id=acknowledgement_role_id,
            correlation_id=correlation_id,
        )

    def _source_anchor_for_activity(
        self,
        activity: dict[str, Any],
        logical_channel: str,
    ) -> SourceAnchor:
        from_user = activity.get("from") or {}
        channel_data = activity.get("channelData") or {}
        conversation = activity.get("conversation") or {}
        channel = channel_data.get("channel") or {}
        display_label = (
            f"{from_user.get('name') or 'teams-user'} in "
            f"{channel.get('name') or logical_channel}"
        )
        return SourceAnchor(
            connector_type="teams",
            connector_id=self.connector_id,
            source_scope=logical_channel,
            source_message_id=(
                str(activity.get("id")) if activity.get("id") is not None else None
            ),
            actor=str(from_user.get("name") or "teams-user"),
            received_at=utc_now_iso(),
            display_label=display_label,
            extensions={
                "conversation_ref": _short_ref(str(conversation.get("id") or "")),
                "channel_ref": _short_ref(str(channel.get("id") or "")),
                "actor_ref": _short_ref(str(from_user.get("id") or "")),
            },
        )

    def _source_idempotency_key(self, activity: dict[str, Any], logical_channel: str) -> str:
        graph = activity.get("graph") if isinstance(activity.get("graph"), dict) else {}
        source_message_id = (
            graph.get("message_id")
            or activity.get("id")
            or self._message_fingerprint_for_activity(activity)
        )
        channel_data = activity.get("channelData") or {}
        channel = channel_data.get("channel") or {}
        channel_id = channel.get("id") or (activity.get("conversation") or {}).get("id")
        return f"teams:{logical_channel}:{channel_id}:{source_message_id}"

    @staticmethod
    def _message_fingerprint_for_activity(activity: dict[str, Any]) -> str:
        from_user = activity.get("from") or {}
        text = re.sub(
            r"\s+",
            " ",
            _plain_text(activity.get("text")).casefold(),
        ).strip()
        return hashlib.sha256(
            "|".join(
                [
                    str(from_user.get("id") or from_user.get("name") or ""),
                    text,
                    str(activity.get("timestamp") or ""),
                ]
            ).encode("utf-8")
        ).hexdigest()[:24]

    @staticmethod
    def _targeted_request_should_enter_queue(roles: list[str], text: str) -> bool:
        if roles != ["delivery-manager"]:
            return False
        value = text.casefold()
        work_terms = {
            "slice",
            "feature",
            "story",
            "work item",
            "work-item",
            "implementation",
            "build",
            "run",
        }
        coordination_terms = {"create", "start", "run", "coordinate", "get the team"}
        return any(term in value for term in work_terms) and any(
            term in value for term in coordination_terms
        )

    @staticmethod
    def _recommended_sponsor_work_item_type(
        text: str,
        *,
        default_work_item_type: str,
    ) -> str:
        value = text.casefold()
        if any(term in value for term in {"slice", "implementation", "build", "feature"}):
            return "slice"
        if any(term in value for term in {"research", "investigate", "spike"}):
            return "spike"
        return default_work_item_type

    def _activity_mentions(self, activity: dict[str, Any], mention: str) -> bool:
        mention_key = _normalise_mention_text(mention)
        return mention_key in {
            _normalise_mention_text(text)
            for text in _mention_texts_from_activity(activity)
        }

    def _mentioned_role_ids(self, activity: dict[str, Any]) -> list[str]:
        assert self.connector_config is not None
        mentioned_roles = _role_ids_for_mentions(
            _mention_texts_from_activity(activity),
            self.connector_config,
        )
        if mentioned_roles:
            return mentioned_roles
        return _role_ids_for_leading_address(
            _plain_text(activity.get("text")),
            self.connector_config,
        )

    def _logical_channel_for_activity(self, activity: dict[str, Any]) -> str | None:
        if self.connector_config is None:
            return None
        channel_data = activity.get("channelData") or {}
        channel = channel_data.get("channel") or {}
        conversation = activity.get("conversation") or {}
        candidates = {
            str(value)
            for value in [
                channel.get("id"),
                channel.get("name"),
                conversation.get("id"),
            ]
            if value
        }
        for logical_channel, channel_config in self.connector_config.channels.items():
            if (
                logical_channel in candidates
                or channel_config.channel_id in candidates
                or channel_config.name in candidates
            ):
                return logical_channel
        return None

    def _journal_ignored_channel_message(
        self,
        activity: dict[str, Any],
        *,
        reason: str,
    ) -> None:
        self.journal.append(
            "teams_channel_message_ignored",
            project_id=self.project_id,
            connector_id=self.connector_id,
            activity_id=activity.get("id"),
            activity_type=activity.get("type"),
            conversation_id=(activity.get("conversation") or {}).get("id"),
            from_id=(activity.get("from") or {}).get("id"),
            reason=reason,
        )

    def _submit_human_response(
        self,
        activity: dict[str, Any],
        value: dict[str, Any],
    ) -> HumanResponseSubmissionResult:
        responder = (
            (activity.get("from") or {}).get("name")
            or (activity.get("from") or {}).get("id")
            or "teams-user"
        )
        response_value = value.get("response_value")
        trace_context = (
            value.get("trace_context")
            if isinstance(value.get("trace_context"), dict)
            else None
        )
        wait_seconds = telemetry.elapsed_seconds(value.get("requested_at"))
        wait_attrs = telemetry.span_attributes(
            project_id=self.project_id,
            connector_id=self.connector_id,
            role_id=value.get("role_id"),
            work_item_id=value.get("work_item_id"),
            work_item_type=value.get("work_item_type"),
            lifecycle_state=value.get("lifecycle_state"),
            gate_id=value.get("gate_id"),
            response_request_id=value.get("response_request_id"),
            response_value=response_value,
            correlation_id=value.get("correlation_id"),
        )
        with telemetry.start_span(
            "human_response.wait",
            correlation_id=value.get("correlation_id"),
            trace_context=trace_context,
            attributes=wait_attrs,
        ):
            if wait_seconds is not None:
                telemetry.record_duration(
                    "agentic_mesh.human_response.wait.duration",
                    wait_seconds,
                    wait_attrs,
                )
        result = self.human_response_submission_service.submit(
            target_role=str(value["role_id"]),
            work_item_id=str(value["work_item_id"]),
            work_item_type=str(value.get("work_item_type") or "slice"),
            lifecycle_state=str(value["lifecycle_state"]),
            gate_id=str(value["gate_id"]),
            response_type=str(value.get("response_type") or ""),
            approval_request_id=(
                str(value.get("approval_request_id"))
                if value.get("approval_request_id")
                else None
            ),
            response_request_id=str(value["response_request_id"]),
            responder=str(responder),
            response_value=response_value,
            source=f"teams:{self.connector_id}",
            authenticated=True,
            correlation_id=value.get("correlation_id"),
            trace_context=trace_context,
        )
        self.journal.append(
            "human_response_received_from_teams",
            project_id=self.project_id,
            connector_id=self.connector_id,
            message_id=result.message_id,
            work_item_id=value.get("work_item_id"),
            lifecycle_state=value.get("lifecycle_state"),
            gate_id=value.get("gate_id"),
            approval_request_id=value.get("approval_request_id"),
            response_request_id=value.get("response_request_id"),
            validation_reason=result.validation_reason,
            final=result.final,
            duplicate=result.duplicate,
            context_lookup=result.context_lookup,
            correlation_id=value.get("correlation_id"),
        )
        return result

    def _update_original_card(
        self,
        activity: dict[str, Any],
        value: dict[str, Any],
        card: dict[str, Any],
        submission: HumanResponseSubmissionResult,
    ) -> None:
        if self.connector_config is None or self.secrets is None:
            return
        conversation = activity.get("conversation") or {}
        conversation_id = conversation.get("id")
        activity_id = activity.get("replyToId")
        service_url = activity.get("serviceUrl")
        role_id = value.get("role_id")
        if not conversation_id or not activity_id or not service_url or not role_id:
            self.journal.append(
                "teams_bot_card_update_skipped",
                project_id=self.project_id,
                connector_id=self.connector_id,
                message_id=submission.message_id,
                work_item_id=value.get("work_item_id"),
                lifecycle_state=value.get("lifecycle_state"),
                reason="missing_activity_reference",
                correlation_id=submission.correlation_id,
            )
            return

        try:
            with telemetry.start_span(
                "teams.card.update",
                correlation_id=submission.correlation_id,
                trace_context=None,
                attributes=telemetry.span_attributes(
                    project_id=self.project_id,
                    connector_id=self.connector_id,
                    role_id=role_id,
                    conversation_id=conversation_id,
                    activity_id=activity_id,
                    work_item_id=value.get("work_item_id"),
                    lifecycle_state=value.get("lifecycle_state"),
                    correlation_id=submission.correlation_id,
                ),
            ):
                response = self._update_activity(
                    service_url=str(service_url),
                    conversation_id=str(conversation_id),
                    activity_id=str(activity_id),
                    role_id=str(role_id),
                    card=card,
                )
        except Exception as exc:
            self.journal.append(
                "teams_bot_card_update_failed",
                project_id=self.project_id,
                connector_id=self.connector_id,
                message_id=submission.message_id,
                work_item_id=value.get("work_item_id"),
                lifecycle_state=value.get("lifecycle_state"),
                role_id=role_id,
                conversation_id=conversation_id,
                activity_id=activity_id,
                error=str(exc),
                correlation_id=submission.correlation_id,
            )
            return

        self.journal.append(
            "teams_bot_card_updated",
            project_id=self.project_id,
            connector_id=self.connector_id,
            message_id=submission.message_id,
            work_item_id=value.get("work_item_id"),
            lifecycle_state=value.get("lifecycle_state"),
            role_id=role_id,
            conversation_id=conversation_id,
            activity_id=activity_id,
            updated_activity_id=response.get("id"),
            correlation_id=submission.correlation_id,
        )

    def _update_activity(
        self,
        *,
        service_url: str,
        conversation_id: str,
        activity_id: str,
        role_id: str,
        card: dict[str, Any],
    ) -> dict[str, Any]:
        role_bot = self.connector_config.role_bots[role_id]  # type: ignore[union-attr]
        app_id = self.secrets.get(role_bot.bot_id_ref)  # type: ignore[union-attr]
        app_secret = self.secrets.get(role_bot.secret_ref)  # type: ignore[union-attr]
        token = bot_framework_token(
            tenant_id=self.connector_config.tenant_id,  # type: ignore[union-attr]
            app_id=app_id,
            app_secret=app_secret,
        )
        update_url = (
            f"{service_url.rstrip('/')}/v3/conversations/"
            f"{quote(conversation_id, safe='')}/activities/"
            f"{quote(activity_id, safe='')}"
        )
        body = json.dumps(
            {
                "type": "message",
                "attachments": [
                    {
                        "contentType": "application/vnd.microsoft.card.adaptive",
                        "content": card,
                    }
                ],
            }
        ).encode("utf-8")
        req = request.Request(
            update_url,
            data=body,
            method="PUT",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with request.urlopen(req, timeout=30) as response:
                response_body = response.read().decode("utf-8")
        except HTTPError as exc:
            error_body = exc.read().decode("utf-8")
            raise RuntimeError(f"Bot activity update returned {exc.code}: {error_body}") from exc
        return json.loads(response_body) if response_body else {}

    def _incoming_path(self, activity_id: str) -> Path:
        return (
            self.state_root
            / "projects"
            / self.project_id
            / "connectors"
            / "teams"
            / "incoming"
            / f"{activity_id}.json"
        )

    @staticmethod
    def _activity_id(activity: dict[str, Any]) -> str:
        activity_id = activity.get("id")
        if isinstance(activity_id, str) and activity_id:
            return activity_id.replace("/", "_").replace("\\", "_")
        return ConnectorMessage.create(
            channel="teams-incoming",
            message_type="teams.activity",
            payload={},
            source="teams",
        ).message_id


class GraphTeamsChannelIngressAdapter:
    """Reads Teams channel messages through Graph and routes them into intake."""

    def __init__(
        self,
        *,
        connector_id: str,
        project_id: str,
        state_root: Path,
        connector_config: ProjectConnectorConfig,
        message_store: FileMessageStore,
        journal: EventJournal,
        project_config: ProjectConfig,
        token: str,
        connector_outbox: FileConnectorOutbox | None = None,
    ) -> None:
        self.connector_id = connector_id
        self.project_id = project_id
        self.state_root = state_root
        self.connector_config = connector_config
        self.message_store = message_store
        self.journal = journal
        self.project_config = project_config
        self.token = token
        self.connector_outbox = connector_outbox
        self.ingress = TeamsBotIngress(
            connector_id=connector_id,
            project_id=project_id,
            state_root=state_root,
            message_store=message_store,
            journal=journal,
            connector_config=connector_config,
            project_config=project_config,
            connector_outbox=connector_outbox,
        )

    def process_once(self, channel: str, *, max_messages: int = 25) -> dict[str, int]:
        seen = self._seen_message_ids(channel)
        routed = 0
        skipped = 0
        new_seen = set(seen)

        root_messages = sorted(
            self._list_channel_messages(channel, max_messages=max_messages),
            key=lambda item: str(item.get("createdDateTime") or item.get("id") or ""),
        )
        messages: list[dict[str, Any]] = []
        for root_message in root_messages:
            messages.append(root_message)
            root_message_id = str(root_message.get("id") or "")
            if not root_message_id:
                continue
            try:
                replies = self._list_channel_replies(
                    channel,
                    root_message_id,
                    max_messages=max_messages,
                )
            except Exception as exc:
                self.journal.append(
                    "teams_graph_channel_replies_fetch_failed",
                    project_id=self.project_id,
                    connector_id=self.connector_id,
                    channel=channel,
                    parent_message_ref=_short_ref(root_message_id),
                    error=exc.__class__.__name__,
                )
                replies = []
            for reply in replies:
                reply = dict(reply)
                reply.setdefault("parent_message_id", root_message_id)
                messages.append(reply)
        messages = sorted(
            messages,
            key=lambda item: str(item.get("createdDateTime") or item.get("id") or ""),
        )
        unseen_messages = [
            graph_message
            for graph_message in messages
            if str(graph_message.get("id") or "")
            and str(graph_message.get("id") or "") not in seen
        ]
        latest_by_fingerprint = {
            fingerprint: str(graph_message.get("id"))
            for graph_message in unseen_messages
            if (fingerprint := self._message_fingerprint(graph_message))
        }

        for graph_message in unseen_messages:
            graph_message_id = str(graph_message.get("id") or "")
            fingerprint = self._message_fingerprint(graph_message)
            if (
                fingerprint
                and latest_by_fingerprint.get(fingerprint) != graph_message_id
            ):
                self._journal_skipped(
                    channel,
                    graph_message,
                    reason="duplicate_message_superseded",
                )
                skipped += 1
                new_seen.add(graph_message_id)
                continue

            raw_path = self._raw_graph_message_path(channel, graph_message_id)
            self._write_json(raw_path, graph_message)
            self.journal.append(
                "teams_graph_channel_message_received",
                project_id=self.project_id,
                connector_id=self.connector_id,
                channel=channel,
                teams_message_id=graph_message_id,
                teams_channel_id=self.connector_config.channels[channel].channel_id,
                teams_team_id=self.connector_config.team_id,
                raw_message_path=str(raw_path),
            )

            if self._should_skip_message(channel, graph_message):
                skipped += 1
                new_seen.add(graph_message_id)
                continue

            activity = self._activity_from_graph_message(channel, graph_message)
            messages = self.ingress._record_channel_intake(activity, raw_path)
            if messages is None:
                skipped += 1
            else:
                routed += 1 if isinstance(messages, Message) else len(messages)
            new_seen.add(graph_message_id)

        self._write_cursor(channel, new_seen)
        return {"routed": routed, "skipped": skipped, "seen": len(new_seen)}

    def _message_fingerprint(self, graph_message: dict[str, Any]) -> str:
        text = _plain_text(self._message_text(graph_message)).casefold()
        return re.sub(r"\s+", " ", text).strip()

    def _list_channel_messages(
        self,
        channel: str,
        *,
        max_messages: int,
    ) -> list[dict[str, Any]]:
        channel_config = self.connector_config.channels[channel]
        url = (
            "https://graph.microsoft.com/v1.0/teams/"
            f"{quote(self.connector_config.team_id, safe='')}/channels/"
            f"{quote(channel_config.channel_id, safe='')}/messages?"
            f"{urlencode({'$top': max_messages})}"
        )
        req = request.Request(
            url,
            method="GET",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        try:
            with request.urlopen(req, timeout=30) as response:
                response_body = response.read().decode("utf-8")
        except HTTPError as exc:
            error_body = exc.read().decode("utf-8")
            raise RuntimeError(f"Graph returned {exc.code}: {error_body}") from exc
        payload = json.loads(response_body) if response_body else {}
        value = payload.get("value") if isinstance(payload, dict) else None
        return [item for item in value or [] if isinstance(item, dict)]

    def _list_channel_replies(
        self,
        channel: str,
        root_message_id: str,
        *,
        max_messages: int,
    ) -> list[dict[str, Any]]:
        channel_config = self.connector_config.channels[channel]
        url = (
            "https://graph.microsoft.com/v1.0/teams/"
            f"{quote(self.connector_config.team_id, safe='')}/channels/"
            f"{quote(channel_config.channel_id, safe='')}/messages/"
            f"{quote(root_message_id, safe='')}/replies?"
            f"{urlencode({'$top': max_messages})}"
        )
        req = request.Request(
            url,
            method="GET",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        try:
            with request.urlopen(req, timeout=30) as response:
                response_body = response.read().decode("utf-8")
        except HTTPError as exc:
            error_body = exc.read().decode("utf-8")
            raise RuntimeError(f"Graph returned {exc.code}: {error_body}") from exc
        payload = json.loads(response_body) if response_body else {}
        value = payload.get("value") if isinstance(payload, dict) else None
        return [item for item in value or [] if isinstance(item, dict)]

    def _should_skip_message(self, channel: str, graph_message: dict[str, Any]) -> bool:
        text = self._message_text(graph_message)
        if not text:
            self._journal_skipped(channel, graph_message, reason="empty_message")
            return True
        if self._is_connector_echo(graph_message):
            self._journal_skipped(channel, graph_message, reason="connector_echo")
            return True
        if graph_message.get("parent_message_id") or graph_message.get("replyToId"):
            return False
        if (
            channel == "all-agents"
            and not self._message_mentions(graph_message, channel)
            and not self._message_mentions_role(graph_message)
        ):
            self._journal_skipped(channel, graph_message, reason="missing_channel_mention")
            return True
        return False

    def _is_connector_echo(self, graph_message: dict[str, Any]) -> bool:
        sender = graph_message.get("from") or {}
        application = sender.get("application") or {}
        user = sender.get("user") or {}
        sender_name = str(
            application.get("displayName") or user.get("displayName") or ""
        )
        plain_text = _plain_text(self._message_text(graph_message)).strip()
        if application:
            return True
        if sender_name.casefold().startswith("am-"):
            return True
        return plain_text.startswith("Agentic Mesh received:")

    def _activity_from_graph_message(
        self,
        channel: str,
        graph_message: dict[str, Any],
    ) -> dict[str, Any]:
        channel_config = self.connector_config.channels[channel]
        user = ((graph_message.get("from") or {}).get("user") or {})
        application = ((graph_message.get("from") or {}).get("application") or {})
        from_identity = user or application
        return {
            "type": "message",
            "id": graph_message.get("id"),
            "replyToId": graph_message.get("parent_message_id")
            or graph_message.get("replyToId"),
            "serviceUrl": "graph://microsoft-teams",
            "timestamp": graph_message.get("createdDateTime"),
            "text": self._message_text(graph_message),
            "from": {
                "id": from_identity.get("id"),
                "name": from_identity.get("displayName"),
            },
            "conversation": {"id": channel_config.channel_id},
            "channelData": {
                "team": {"id": self.connector_config.team_id},
                "channel": {"id": channel_config.channel_id, "name": channel_config.name},
            },
            "graph": {
                "message_id": graph_message.get("id"),
                "parent_message_id": graph_message.get("parent_message_id")
                or graph_message.get("replyToId"),
                "web_url": graph_message.get("webUrl"),
                "mentions": graph_message.get("mentions") or [],
            },
        }

    def _message_mentions(self, graph_message: dict[str, Any], mention: str) -> bool:
        mention_key = mention.casefold()
        mentions = graph_message.get("mentions") or []
        for item in mentions:
            if not isinstance(item, dict):
                continue
            mention_text = _plain_text(item.get("mentionText")).casefold()
            if mention_text == mention_key:
                return True
        html_text = self._message_text(graph_message)
        if "<at" not in html_text.casefold():
            return False
        return mention_key in _plain_text(html_text).casefold().split()

    def _message_mentions_role(self, graph_message: dict[str, Any]) -> bool:
        mentioned_roles = _role_ids_for_mentions(
            _mention_texts_from_graph_message(graph_message),
            self.connector_config,
        )
        if mentioned_roles:
            return True
        return bool(
            _role_ids_for_leading_address(
                self._message_text(graph_message),
                self.connector_config,
            )
        )

    @staticmethod
    def _message_text(graph_message: dict[str, Any]) -> str:
        body = graph_message.get("body") or {}
        content = body.get("content") if isinstance(body, dict) else None
        if isinstance(content, str) and content.strip():
            return content
        subject = graph_message.get("subject")
        return subject if isinstance(subject, str) else ""

    def _journal_skipped(
        self,
        channel: str,
        graph_message: dict[str, Any],
        *,
        reason: str,
    ) -> None:
        self.journal.append(
            "teams_graph_channel_message_skipped",
            project_id=self.project_id,
            connector_id=self.connector_id,
            channel=channel,
            teams_message_id=graph_message.get("id"),
            reason=reason,
        )

    def _cursor_path(self, channel: str) -> Path:
        return (
            self.state_root
            / "projects"
            / self.project_id
            / "connectors"
            / "teams"
            / "graph_ingress"
            / channel
            / "cursor.json"
        )

    def _raw_graph_message_path(self, channel: str, graph_message_id: str) -> Path:
        safe_id = graph_message_id.replace("/", "_").replace("\\", "_")
        return (
            self.state_root
            / "projects"
            / self.project_id
            / "connectors"
            / "teams"
            / "graph_ingress"
            / channel
            / "incoming"
            / f"{safe_id}.json"
        )

    def _seen_message_ids(self, channel: str) -> set[str]:
        path = self._cursor_path(channel)
        if not path.exists():
            return set()
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        seen = data.get("seen_message_ids") if isinstance(data, dict) else None
        return {str(item) for item in seen or []}

    def _write_cursor(self, channel: str, seen_message_ids: set[str]) -> None:
        path = self._cursor_path(channel)
        self._write_json(path, {"seen_message_ids": sorted(seen_message_ids)[-500:]})

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")


def _plain_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = re.sub(r"<[^>]+>", " ", value)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _mention_texts_from_activity(activity: dict[str, Any]) -> list[str]:
    texts: list[str] = []
    for entity in activity.get("entities") or []:
        if not isinstance(entity, dict) or entity.get("type") != "mention":
            continue
        texts.append(_plain_text(entity.get("text")))
        mentioned = entity.get("mentioned") or {}
        if isinstance(mentioned, dict):
            texts.append(_plain_text(mentioned.get("name")))
    graph = activity.get("graph") or {}
    if isinstance(graph, dict):
        for item in graph.get("mentions") or []:
            if isinstance(item, dict):
                texts.append(_plain_text(item.get("mentionText")))
    activity_text = activity.get("text")
    texts.extend(_at_mention_texts(activity_text if isinstance(activity_text, str) else ""))
    return [text for text in texts if text]


def _mention_texts_from_graph_message(graph_message: dict[str, Any]) -> list[str]:
    texts: list[str] = []
    for item in graph_message.get("mentions") or []:
        if isinstance(item, dict):
            texts.append(_plain_text(item.get("mentionText")))
    texts.extend(_at_mention_texts(GraphTeamsChannelIngressAdapter._message_text(graph_message)))
    return [text for text in texts if text]


def _at_mention_texts(value: str) -> list[str]:
    if not value or "<at" not in value.casefold():
        return []
    return [
        _plain_text(match)
        for match in re.findall(r"<at\b[^>]*>(.*?)</at>", value, flags=re.IGNORECASE)
    ]


def _normalise_mention_text(value: Any) -> str:
    text = _plain_text(value)
    text = text.removeprefix("@").strip()
    return re.sub(r"\s+", " ", text).casefold()


def _role_ids_for_mentions(
    mention_texts: list[str],
    connector_config: ProjectConnectorConfig,
) -> list[str]:
    mention_keys = {_normalise_mention_text(text) for text in mention_texts}
    matched: list[str] = []
    for role_id in sorted(connector_config.role_bots):
        alias_keys = _role_alias_keys(role_id, connector_config)
        if mention_keys & alias_keys:
            matched.append(role_id)
    return matched


def _role_ids_for_leading_address(
    text: str,
    connector_config: ProjectConnectorConfig,
) -> list[str]:
    plain_text = _normalise_leading_address_text(text)
    if not plain_text:
        return []
    matched: list[str] = []
    for role_id in sorted(connector_config.role_bots):
        for alias_key in _role_alias_keys(role_id, connector_config):
            if _leading_address_matches(plain_text, alias_key):
                matched.append(role_id)
                break
    return matched


def _role_alias_keys(
    role_id: str,
    connector_config: ProjectConnectorConfig,
) -> set[str]:
    role_bot = connector_config.role_bots[role_id]
    aliases = {
        role_id,
        role_id.replace("-", " "),
        role_bot.display_name,
    }
    if role_bot.display_name.casefold().startswith("am-"):
        aliases.add(role_bot.display_name[3:])
    return {_normalise_mention_text(alias) for alias in aliases}


def _normalise_leading_address_text(text: str) -> str:
    text = _plain_text(text)
    text = text.strip()
    text = re.sub(r"^[@\s]+", "", text)
    return re.sub(r"\s+", " ", text).casefold()


def _leading_address_matches(text: str, alias_key: str) -> bool:
    if text == alias_key:
        return True
    return any(
        text.startswith(f"{alias_key}{separator}")
        for separator in (" ", ":", ",", ".", "\n", "\t")
    )


def _title_from_text(text: str) -> str:
    if not text:
        return "Teams message"
    if len(text) <= 80:
        return text
    return f"{text[:77].rstrip()}..."


def _direct_work_branch_name(work_item_id: str, title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    slug = slug[:40].strip("-") or "direct-work"
    suffix = work_item_id.removeprefix("work-")[:12] or work_item_id[:12]
    return f"codex/{suffix}-{slug}"
