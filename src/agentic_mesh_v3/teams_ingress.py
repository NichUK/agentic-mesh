from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from typing import Protocol

from agentic_mesh_v3.authority import role_from_instance
from agentic_mesh_v3.broker import BrokerAdapter
from agentic_mesh_v3.connectors import StakeholderBridge
from agentic_mesh_v3.connectors import StakeholderMessage
from agentic_mesh_v3.db import V3Database
from agentic_mesh_v3.project_config import V3ProjectConfig


@dataclass(frozen=True)
class TeamsRoleIdentity:
    role_id: str
    display_name: str
    bot_id: str | None = None


class ConversationRecorder(Protocol):
    def record(self, message: StakeholderMessage) -> None:
        """Persist an inbound stakeholder message for later agent context."""

    def record_route_result(
        self,
        message: StakeholderMessage,
        *,
        subjects: tuple[str, ...],
        status: str,
        error: str | None = None,
    ) -> None:
        """Persist the outcome of routing a captured message to agent inboxes."""


class ApprovalResponseRecorder(Protocol):
    def record(self, message: StakeholderMessage) -> bool:
        """Record an approval response if the stakeholder message contains one."""


class StakeholderQuestionResponseRecorder(Protocol):
    def record(self, message: StakeholderMessage) -> bool:
        """Record a stakeholder answer if the message references a question."""


class DatabaseConversationRecorder:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)

    def record(self, message: StakeholderMessage) -> None:
        db = V3Database(self.db_path)
        try:
            db.migrate()
            db.record_conversation_message(
                message_id=message.message_id,
                connector=message.connector,
                conversation_ref=message.conversation_ref,
                source_type=message.source_type,
                sender_ref=message.sender_ref,
                text=message.text,
                thread_ref=message.thread_ref,
                mentioned_roles=message.mentioned_roles,
            )
        finally:
            db.close()

    def record_route_result(
        self,
        message: StakeholderMessage,
        *,
        subjects: tuple[str, ...],
        status: str,
        error: str | None = None,
    ) -> None:
        db = V3Database(self.db_path)
        try:
            db.migrate()
            with db.connection:
                db.record_event(
                    "teams_activity.route_result",
                    "conversation",
                    message.message_id,
                    {
                        "connector": message.connector,
                        "conversation_ref": message.conversation_ref,
                        "source_type": message.source_type,
                        "subjects": list(subjects),
                        "status": status,
                        "error": error,
                    },
                )
        finally:
            db.close()


class DatabaseApprovalResponseRecorder:
    """Persist approval responses found in inbound stakeholder messages.

    This keeps Teams/Bot Framework approval replies in the same durable path as
    the CLI approval command while still letting the original message route to
    the addressed role agent for context.
    """

    def __init__(self, db_path: Path, *, broker: BrokerAdapter | None = None, stream: str = "agent-inbox") -> None:
        self.db_path = Path(db_path)
        self.broker = broker
        self.stream = stream

    def record(self, message: StakeholderMessage) -> bool:
        response = approval_response_from_text(message.text)
        if response is None:
            return False
        approval_id, status = response
        db = V3Database(self.db_path)
        try:
            db.migrate()
            try:
                db.record_approval_response(
                    approval_id=approval_id,
                    status=status,
                    response=message.text,
                    responder_ref=message.sender_ref,
                )
            except ValueError:
                return False
            approval = db.approval_detail(approval_id)
        finally:
            db.close()
        if approval is not None and self.broker is not None:
            requested_by_role = str(approval["requested_by_role"])
            self.broker.ensure_stream(self.stream, [f"agent.{requested_by_role}"])
            self.broker.publish(
                self.stream,
                f"agent.{requested_by_role}",
                {
                    "message_type": "approval.response_recorded",
                    "approval_id": str(approval["approval_id"]),
                    "work_item_id": str(approval["work_item_id"]),
                    "status": str(approval["status"]),
                    "response": str(approval.get("response") or ""),
                    "responder_ref": message.sender_ref,
                    "source_message_id": message.message_id,
                    "conversation_ref": message.conversation_ref,
                    "reply_target_ref": message.reply_target_ref,
                    "reply_thread_ref": message.reply_thread_ref,
                },
            )
        return True


class DatabaseStakeholderQuestionResponseRecorder:
    """Persist answers to stakeholder questions and wake the asking role.

    The runtime captures that a human answered a specific recorded question. It
    does not interpret the answer or decide the next lifecycle action; the
    asking role agent receives the answer in its inbox and continues the work.
    """

    def __init__(self, db_path: Path, *, broker: BrokerAdapter | None = None, stream: str = "agent-inbox") -> None:
        self.db_path = Path(db_path)
        self.broker = broker
        self.stream = stream

    def record(self, message: StakeholderMessage) -> bool:
        question_id = stakeholder_question_response_id_from_text(message.text)
        if question_id is None:
            return False
        db = V3Database(self.db_path)
        try:
            db.migrate()
            try:
                response = db.record_governance_response(
                    record_id=question_id,
                    status="answered",
                    response=message.text,
                    responder_ref=message.sender_ref,
                )
            except ValueError:
                return False
        finally:
            db.close()
        if self.broker is not None:
            target_role = role_from_instance(str(response["role_instance_id"]))
            self.broker.ensure_stream(self.stream, [f"agent.{target_role}"])
            self.broker.publish(
                self.stream,
                f"agent.{target_role}",
                {
                    "message_type": "stakeholder.question_answered",
                    "question_id": str(response["record_id"]),
                    "work_item_id": str(response["work_item_id"]),
                    "status": str(response["status"]),
                    "response": str(response["response"]),
                    "responder_ref": message.sender_ref,
                    "source_message_id": message.message_id,
                    "conversation_ref": message.conversation_ref,
                    "reply_target_ref": message.reply_target_ref,
                    "reply_thread_ref": message.reply_thread_ref,
                },
            )
        return True


class TeamsActivityRouter:
    """Route Bot Framework Teams activities into the connector-neutral bridge."""

    def __init__(
        self,
        bridge: StakeholderBridge,
        *,
        role_identities: tuple[TeamsRoleIdentity, ...] = (),
        conversation_recorder: ConversationRecorder | None = None,
        approval_response_recorder: ApprovalResponseRecorder | None = None,
        question_response_recorder: StakeholderQuestionResponseRecorder | None = None,
    ) -> None:
        self.bridge = bridge
        self.role_identities = role_identities
        self.conversation_recorder = conversation_recorder
        self.approval_response_recorder = approval_response_recorder
        self.question_response_recorder = question_response_recorder

    def route_activity(self, activity: dict[str, Any]) -> list[str]:
        message = normalize_teams_activity(activity, role_identities=self.role_identities)
        if self.conversation_recorder is not None:
            self.conversation_recorder.record(message)
        if self.approval_response_recorder is not None:
            self.approval_response_recorder.record(message)
        if self.question_response_recorder is not None:
            self.question_response_recorder.record(message)
        try:
            subjects = self.bridge.route_inbound(message)
        except Exception as exc:
            if self.conversation_recorder is not None:
                self.conversation_recorder.record_route_result(
                    message,
                    subjects=(),
                    status="failed",
                    error=f"{type(exc).__name__}: {exc}",
                )
            raise
        if self.conversation_recorder is not None:
            self.conversation_recorder.record_route_result(
                message,
                subjects=tuple(subjects),
                status="routed" if subjects else "no_route",
            )
        return subjects


def teams_role_identities_from_project_config(config: V3ProjectConfig) -> tuple[TeamsRoleIdentity, ...]:
    identities: list[TeamsRoleIdentity] = []
    for role in config.roles:
        identity = role.messaging_identity
        if not identity.display_name:
            continue
        identities.append(
            TeamsRoleIdentity(
                role_id=role.role_id,
                display_name=identity.display_name,
                bot_id=identity.bot_id_ref,
            )
        )
    return tuple(identities)


def normalize_teams_activity(
    activity: dict[str, Any],
    *,
    role_identities: tuple[TeamsRoleIdentity, ...] = (),
) -> StakeholderMessage:
    """Convert a Teams Bot Framework activity into connector-neutral V3 input."""

    conversation = _mapping(activity.get("conversation"))
    conversation_type = str(conversation.get("conversationType") or "")
    source_type = "dm" if conversation_type == "personal" else "channel"
    role_by_name = {identity.display_name.casefold(): identity.role_id for identity in role_identities}
    role_by_bot_id = {
        identity.bot_id: identity.role_id
        for identity in role_identities
        if identity.bot_id
    }
    mentioned_roles = _mentioned_roles(activity, role_by_name=role_by_name, role_by_bot_id=role_by_bot_id)
    recipient_role = _recipient_role(activity, role_by_name=role_by_name, role_by_bot_id=role_by_bot_id)
    team_id, channel_id = _team_channel_ids(activity)

    return StakeholderMessage(
        connector="teams",
        message_id=str(activity.get("id") or ""),
        source_type=source_type,
        sender_ref=_sender_ref(activity),
        conversation_ref=_conversation_ref(activity, source_type=source_type, recipient_role=recipient_role),
        text=_clean_text(str(activity.get("text") or "")),
        mentioned_roles=mentioned_roles,
        thread_ref=_optional_text(activity.get("replyToId")),
        reply_target_ref=_teams_reply_target_ref(
            activity,
            source_type=source_type,
            team_id=team_id,
            channel_id=channel_id,
        ),
        reply_thread_ref=_teams_reply_thread_ref(activity, source_type=source_type),
    )


def approval_response_from_text(text: str) -> tuple[str, str] | None:
    approval_id_match = re.search(r"\b(?:approval|human-response)-[A-Za-z0-9_-]+\b", text)
    if approval_id_match is None:
        return None
    normalised = re.sub(r"[^a-z0-9]+", " ", text.casefold())
    if re.search(r"\bchanges?\s+requested\b", normalised) or "changes requested" in normalised:
        status = "changes_requested"
    elif re.search(r"\breject(?:ed)?\b", normalised):
        status = "rejected"
    elif re.search(r"\bapprov(?:e|ed)\b", normalised):
        status = "approved"
    else:
        return None
    return approval_id_match.group(0), status


def stakeholder_question_response_id_from_text(text: str) -> str | None:
    match = re.search(r"\bquestion-[A-Za-z0-9_-]+\b", text)
    if match is None:
        return None
    return match.group(0)


def _mentioned_roles(
    activity: dict[str, Any],
    *,
    role_by_name: dict[str, str],
    role_by_bot_id: dict[str, str],
) -> tuple[str, ...]:
    roles: list[str] = []
    for entity in activity.get("entities") or ():
        if not isinstance(entity, dict) or entity.get("type") != "mention":
            continue
        mentioned = _mapping(entity.get("mentioned"))
        role = role_by_bot_id.get(_optional_text(mentioned.get("id")) or "")
        if role is None:
            mention_name = str(mentioned.get("name") or "")
            role = role_by_name.get(mention_name.casefold()) or _fallback_role_from_teams_name(mention_name)
        if role and role not in roles:
            roles.append(role)
    return tuple(roles)


def _recipient_role(
    activity: dict[str, Any],
    *,
    role_by_name: dict[str, str],
    role_by_bot_id: dict[str, str],
) -> str | None:
    recipient = _mapping(activity.get("recipient"))
    role = role_by_bot_id.get(_optional_text(recipient.get("id")) or "")
    if role is not None:
        return role
    recipient_name = str(recipient.get("name") or "")
    return role_by_name.get(recipient_name.casefold()) or _fallback_role_from_teams_name(recipient_name)


def _conversation_ref(activity: dict[str, Any], *, source_type: str, recipient_role: str | None) -> str:
    if source_type == "dm":
        return f"dm:{recipient_role}" if recipient_role else f"dm:{_conversation_id(activity)}"
    team_id, channel_id = _team_channel_ids(activity)
    return f"team:{team_id}/channel:{channel_id}"


def _team_channel_ids(activity: dict[str, Any]) -> tuple[str, str]:
    channel_data = _mapping(activity.get("channelData"))
    team = _mapping(channel_data.get("team"))
    channel = _mapping(channel_data.get("channel"))
    team_id = str(team.get("id") or "unknown-team")
    channel_id = str(channel.get("id") or _conversation_id(activity))
    return team_id, channel_id


def _teams_reply_target_ref(
    activity: dict[str, Any],
    *,
    source_type: str,
    team_id: str,
    channel_id: str,
) -> str:
    if source_type == "dm":
        return f"chat:{_conversation_id(activity)}"
    return f"team:{team_id}/channel:{channel_id}"


def _teams_reply_thread_ref(activity: dict[str, Any], *, source_type: str) -> str | None:
    if source_type == "dm":
        return None
    return _optional_text(activity.get("replyToId")) or _optional_text(activity.get("id"))


def _sender_ref(activity: dict[str, Any]) -> str:
    sender = _mapping(activity.get("from"))
    return str(sender.get("aadObjectId") or sender.get("id") or sender.get("name") or "unknown")


def _conversation_id(activity: dict[str, Any]) -> str:
    conversation = _mapping(activity.get("conversation"))
    return str(conversation.get("id") or "unknown-conversation")


def _clean_text(text: str) -> str:
    return re.sub(r"</?at>", "", text).strip()


def _fallback_role_from_teams_name(display_name: str) -> str | None:
    text = display_name.strip()
    for prefix in ("AM-", "AM ", "Agentic Mesh ", "Agentic-Mesh "):
        if text.casefold().startswith(prefix.casefold()):
            role = text[len(prefix) :].strip()
            role_id = re.sub(r"[^a-z0-9]+", "-", role.casefold()).strip("-")
            return role_id or None
    return None


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None
