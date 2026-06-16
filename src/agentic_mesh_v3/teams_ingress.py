from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from typing import Protocol

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


class TeamsActivityRouter:
    """Route Bot Framework Teams activities into the connector-neutral bridge."""

    def __init__(
        self,
        bridge: StakeholderBridge,
        *,
        role_identities: tuple[TeamsRoleIdentity, ...] = (),
        conversation_recorder: ConversationRecorder | None = None,
    ) -> None:
        self.bridge = bridge
        self.role_identities = role_identities
        self.conversation_recorder = conversation_recorder

    def route_activity(self, activity: dict[str, Any]) -> list[str]:
        message = normalize_teams_activity(activity, role_identities=self.role_identities)
        if self.conversation_recorder is not None:
            self.conversation_recorder.record(message)
        return self.bridge.route_inbound(message)


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
            role = role_by_name.get(str(mentioned.get("name") or "").casefold())
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
    return role_by_name.get(str(recipient.get("name") or "").casefold())


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


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None
