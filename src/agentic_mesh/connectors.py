from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib import request
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.parse import urlencode
import html

from agentic_mesh import telemetry
from agentic_mesh.journal import EventJournal
from agentic_mesh.messaging import MESSAGE_TYPE_HUMAN_RESPONSE_REQUESTED
from agentic_mesh.messaging import MESSAGE_TYPE_SDLC_HANDOFF
from agentic_mesh.messaging import build_human_response_received_message
from agentic_mesh.models import ConnectorMessage
from agentic_mesh.models import Message
from agentic_mesh.models import ProjectConnectorConfig
from agentic_mesh.models import ProjectConfig
from agentic_mesh.models import new_id
from agentic_mesh.storage import FileMessageStore
from agentic_mesh.storage import FileConnectorOutbox


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
            "response_request_id": payload.get("response_request_id"),
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
                    error=str(exc),
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
            error_body = exc.read().decode("utf-8")
            raise RuntimeError(f"Graph returned {exc.code}: {error_body}") from exc
        return json.loads(response_body) if response_body else {}

    @staticmethod
    def _render_html(message: ConnectorMessage) -> str:
        if message.type == MESSAGE_TYPE_SDLC_HANDOFF:
            return render_sdlc_handoff_html(message)
        if message.type == MESSAGE_TYPE_HUMAN_RESPONSE_REQUESTED:
            return render_human_response_request_html(message)
        return (
            "<p><strong>Agentic Mesh message</strong></p>"
            f"<pre>{html.escape(json.dumps(message.payload, indent=2))}</pre>"
        )


def load_graph_token(token: str | None, token_file: Path | None) -> str:
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
    raise ValueError("Graph Teams connector requires a token or token file")


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
        error_body = exc.read().decode("utf-8")
        raise RuntimeError(f"Bot token request returned {exc.code}: {error_body}") from exc
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
                    error=str(exc),
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
            error_body = exc.read().decode("utf-8")
            raise RuntimeError(f"Bot Connector returned {exc.code}: {error_body}") from exc
        return json.loads(response_body) if response_body else {}

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

    @staticmethod
    def _render_text(message: ConnectorMessage) -> str:
        if message.type == MESSAGE_TYPE_SDLC_HANDOFF:
            payload = message.payload
            title = html.escape(str(payload.get("title") or "SDLC handoff"))
            work_item_id = html.escape(str(payload.get("work_item_id") or "unknown"))
            source = html.escape(str(payload.get("source_role") or "unknown"))
            target = html.escape(str(payload.get("target_role") or "unknown"))
            source_state = html.escape(str(payload.get("source_lifecycle_state") or "unknown"))
            target_state = html.escape(str(payload.get("target_lifecycle_state") or "unknown"))
            summary = html.escape(str(payload.get("summary") or ""))
            return (
                f"<b>Agentic Mesh SDLC handoff: {title}</b><br/>"
                f"Work item <code>{work_item_id}</code> moved from "
                f"<code>{source_state}</code> ({source}) to "
                f"<code>{target_state}</code> ({target}).<br/>"
                f"{summary}"
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
        return html.escape(json.dumps(message.payload, indent=2))


def render_sdlc_handoff_html(message: ConnectorMessage) -> str:
    payload = message.payload
    title = html.escape(str(payload.get("title") or "SDLC handoff"))
    work_item_id = html.escape(str(payload.get("work_item_id") or "unknown"))
    source = html.escape(str(payload.get("source_role") or "unknown"))
    target = html.escape(str(payload.get("target_role_display_name") or payload.get("target_role") or "unknown"))
    source_state = html.escape(str(payload.get("source_lifecycle_state") or "unknown"))
    target_state = html.escape(str(payload.get("target_lifecycle_state") or "unknown"))
    summary = html.escape(str(payload.get("summary") or ""))
    return (
        f"<p><strong>Agentic Mesh SDLC handoff: {title}</strong></p>"
        f"<p>Work item <code>{work_item_id}</code> moved from "
        f"<code>{source_state}</code> ({source}) to <code>{target_state}</code> "
        f"({target}).</p>"
        f"<p>{summary}</p>"
    )


def render_human_response_request_html(message: ConnectorMessage) -> str:
    payload = message.payload
    prompt = html.escape(str(payload.get("prompt") or "Human response requested"))
    work_item_id = html.escape(str(payload.get("work_item_id") or "unknown"))
    gate_id = html.escape(str(payload.get("gate_id") or "unknown"))
    response_type = html.escape(str(payload.get("response_type") or "unknown"))
    summary = html.escape(str(payload.get("summary") or ""))
    return (
        f"<p><strong>{prompt}</strong></p>"
        f"<p>Work item <code>{work_item_id}</code> is waiting at gate "
        f"<code>{gate_id}</code> for response type <code>{response_type}</code>.</p>"
        f"<p>{summary}</p>"
    )


def build_human_response_card(message: ConnectorMessage) -> dict[str, Any]:
    payload = message.payload
    response_template = payload.get("response_template") or {}
    input_mode = response_template.get("input_mode")
    body = [
        {
            "type": "TextBlock",
            "text": payload.get("prompt") or "Response requested",
            "weight": "Bolder",
            "wrap": True,
        },
        {
            "type": "TextBlock",
            "text": payload.get("summary") or "",
            "wrap": True,
        },
        {
            "type": "FactSet",
            "facts": [
                {"title": "Work item", "value": payload.get("work_item_id") or ""},
                {"title": "Lifecycle", "value": payload.get("lifecycle_state") or ""},
                {"title": "Gate", "value": payload.get("gate_id") or ""},
                {"title": "Response type", "value": payload.get("response_type") or ""},
            ],
        },
    ]
    card: dict[str, Any] = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.5",
        "body": body,
        "actions": [],
    }
    if input_mode == "choice":
        card["actions"] = [
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
        return card

    card["body"].append(input_for_template(response_template))
    card["actions"] = [
        {
            "type": "Action.Submit",
            "title": "Submit",
            "msTeams": {"feedback": {"hide": True}},
            "data": human_response_submit_data(payload),
        }
    ]
    return card


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
        "response_request_id": payload.get("response_request_id"),
        "gate_id": payload.get("gate_id"),
        "response_type": payload.get("response_type"),
        "correlation_id": payload.get("correlation_id"),
        "trace_context": payload.get("trace_context"),
        "requested_at": payload.get("requested_at"),
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
) -> dict[str, Any]:
    response_label = response_value_label(
        submit_payload,
        response_value=response_value,
    )
    return {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.5",
        "body": [
            {
                "type": "TextBlock",
                "text": "Release decision recorded",
                "weight": "Bolder",
                "wrap": True,
            },
            {
                "type": "FactSet",
                "facts": [
                    {
                        "title": "Work item",
                        "value": submit_payload.get("work_item_id") or "",
                    },
                    {
                        "title": "Lifecycle",
                        "value": submit_payload.get("lifecycle_state") or "",
                    },
                    {"title": "Gate", "value": submit_payload.get("gate_id") or ""},
                    {"title": "Decision", "value": response_label},
                    {"title": "Responder", "value": responder},
                ],
            },
        ],
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
    ) -> None:
        self.connector_id = connector_id
        self.project_id = project_id
        self.state_root = state_root
        self.message_store = message_store
        self.journal = journal
        self.connector_config = connector_config
        self.project_config = project_config
        self.secrets = secrets

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
                message = self._record_human_response(activity, value)
                response_card = build_human_response_completed_card(
                    value,
                    responder=str(message.payload.get("responder") or "teams-user"),
                    response_value=message.payload.get("response_value"),
                )
                self.journal.append(
                    "human_response_completion_card_returned",
                    project_id=self.project_id,
                    connector_id=self.connector_id,
                    message_id=message.message_id,
                    work_item_id=message.payload.get("work_item_id"),
                    lifecycle_state=message.payload.get("lifecycle_state"),
                    gate_id=message.payload.get("gate_id"),
                    response_value=message.payload.get("response_value"),
                    responder=message.payload.get("responder"),
                    correlation_id=message.correlation_id,
                )
                self._update_original_card(activity, value, response_card, message)
                return adaptive_card_invoke_response(response_card)

            intake_message = self._record_channel_intake(activity, path)
            if intake_message is not None:
                return {
                    "status": "accepted",
                    "activity_id": activity_id,
                    "routed": True,
                    "message_id": intake_message.message_id,
                    "target_role": intake_message.role_id,
                    "work_item_id": intake_message.payload.get("work_item_id"),
                    "lifecycle_state": intake_message.payload.get("lifecycle_state"),
                }

        return {"status": "accepted", "activity_id": activity_id}

    def _record_channel_intake(
        self,
        activity: dict[str, Any],
        raw_activity_path: Path,
    ) -> Message | None:
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
        if logical_channel is None:
            self._journal_ignored_channel_message(
                activity,
                reason="unmapped_channel",
            )
            return None

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
        work_item_id = new_id("work")
        from_user = activity.get("from") or {}
        channel_data = activity.get("channelData") or {}
        team = channel_data.get("team") or {}
        channel = channel_data.get("channel") or {}
        conversation = activity.get("conversation") or {}
        payload = {
            "title": _title_from_text(text),
            "summary": text,
            "text": text,
            "work_item_id": work_item_id,
            "work_item_type": work_item_type,
            "lifecycle_state": lifecycle_state,
            "source_connector": "teams",
            "source_connector_id": self.connector_id,
            "source_channel": logical_channel,
            "teams_activity_id": activity.get("id"),
            "teams_conversation_id": conversation.get("id"),
            "teams_channel_id": channel.get("id") or conversation.get("id"),
            "teams_team_id": team.get("id"),
            "teams_from_id": from_user.get("id"),
            "teams_from_name": from_user.get("name"),
            "raw_activity_path": str(raw_activity_path),
        }
        message = Message.create(
            role_id=flow_state.owner_role,
            message_type="sponsor_intake.requested",
            payload=payload,
            source=f"teams:{self.connector_id}:{logical_channel}",
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
            teams_activity_id=activity.get("id"),
            teams_conversation_id=conversation.get("id"),
            teams_from_id=from_user.get("id"),
            correlation_id=message.correlation_id,
        )
        return message

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

    def _record_human_response(
        self,
        activity: dict[str, Any],
        value: dict[str, Any],
    ):
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
        message = build_human_response_received_message(
            target_role=str(value["role_id"]),
            work_item_id=str(value["work_item_id"]),
            work_item_type=str(value.get("work_item_type") or "slice"),
            lifecycle_state=str(value["lifecycle_state"]),
            gate_id=str(value["gate_id"]),
            response_request_id=str(value["response_request_id"]),
            responder=str(responder),
            response_value=response_value,
            source=f"teams:{self.connector_id}",
            correlation_id=value.get("correlation_id"),
            trace_context=trace_context,
        )
        message = self.message_store.enqueue(message)
        self.journal.append(
            "human_response_received_from_teams",
            project_id=self.project_id,
            connector_id=self.connector_id,
            message_id=message.message_id,
            work_item_id=message.payload.get("work_item_id"),
            lifecycle_state=message.payload.get("lifecycle_state"),
            gate_id=message.payload.get("gate_id"),
            responder=message.payload.get("responder"),
            correlation_id=message.correlation_id,
        )
        return message

    def _update_original_card(
        self,
        activity: dict[str, Any],
        value: dict[str, Any],
        card: dict[str, Any],
        message,
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
                message_id=message.message_id,
                work_item_id=message.payload.get("work_item_id"),
                lifecycle_state=message.payload.get("lifecycle_state"),
                reason="missing_activity_reference",
                correlation_id=message.correlation_id,
            )
            return

        try:
            with telemetry.start_span(
                "teams.card.update",
                correlation_id=message.correlation_id,
                trace_context=message.trace_context,
                attributes=telemetry.span_attributes(
                    project_id=self.project_id,
                    connector_id=self.connector_id,
                    role_id=role_id,
                    conversation_id=conversation_id,
                    activity_id=activity_id,
                    work_item_id=message.payload.get("work_item_id"),
                    lifecycle_state=message.payload.get("lifecycle_state"),
                    correlation_id=message.correlation_id,
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
                message_id=message.message_id,
                work_item_id=message.payload.get("work_item_id"),
                lifecycle_state=message.payload.get("lifecycle_state"),
                role_id=role_id,
                conversation_id=conversation_id,
                activity_id=activity_id,
                error=str(exc),
                correlation_id=message.correlation_id,
            )
            return

        self.journal.append(
            "teams_bot_card_updated",
            project_id=self.project_id,
            connector_id=self.connector_id,
            message_id=message.message_id,
            work_item_id=message.payload.get("work_item_id"),
            lifecycle_state=message.payload.get("lifecycle_state"),
            role_id=role_id,
            conversation_id=conversation_id,
            activity_id=activity_id,
            updated_activity_id=response.get("id"),
            correlation_id=message.correlation_id,
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


def _plain_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = re.sub(r"<[^>]+>", " ", value)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _title_from_text(text: str) -> str:
    if not text:
        return "Teams message"
    if len(text) <= 80:
        return text
    return f"{text[:77].rstrip()}..."
