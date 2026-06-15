from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from agentic_mesh_v3.connectors import StakeholderMessage


@dataclass(frozen=True)
class TeamsRoleIdentity:
    role_id: str
    display_name: str
    bot_id: str | None = None


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

    return StakeholderMessage(
        connector="teams",
        message_id=str(activity.get("id") or ""),
        source_type=source_type,
        sender_ref=_sender_ref(activity),
        conversation_ref=_conversation_ref(activity, source_type=source_type, recipient_role=recipient_role),
        text=_clean_text(str(activity.get("text") or "")),
        mentioned_roles=mentioned_roles,
        thread_ref=_optional_text(activity.get("replyToId")),
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
    channel_data = _mapping(activity.get("channelData"))
    team = _mapping(channel_data.get("team"))
    channel = _mapping(channel_data.get("channel"))
    team_id = str(team.get("id") or "unknown-team")
    channel_id = str(channel.get("id") or _conversation_id(activity))
    return f"team:{team_id}/channel:{channel_id}"


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
