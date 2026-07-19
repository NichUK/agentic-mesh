from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import re
from typing import Mapping, Sequence

import psycopg
from psycopg.rows import dict_row

from agentic_mesh_v5.database import DatabaseConfigurationError, DatabaseError, SCHEMA
from agentic_mesh_v5.events import DeliveryReceipt, EventDraft, EventStore
from agentic_mesh_v5.events import OutboundDraft, OutboxMessage, StoredEvent
from agentic_mesh_v5.lifecycle import ApprovalRecord
from agentic_mesh_v5.lifecycle import LifecycleAuthorizationError, LifecycleNotFound
from agentic_mesh_v5.progress import reject_sensitive_content
from agentic_mesh_v5.sponsor_approvals import SponsorApprovalCoordinator
from agentic_mesh_v5.teams_connector import ProjectTeamsConnector


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,255}$")
_CARD_STATUSES = frozenset({"approved", "rejected", "expired"})


class TeamsNotificationError(DatabaseError):
    pass


@dataclass(frozen=True, slots=True)
class TeamsApprovalCallbackResult:
    project_id: str
    gate_id: str
    sponsor_id: str
    status: str
    approval_id: str | None

    def to_dict(self) -> dict[str, str | None]:
        return asdict(self)


class TeamsNotificationAdapter:
    """Deliver V5 Teams notification outbox messages idempotently."""

    def __init__(self, database_url: str, connector: ProjectTeamsConnector) -> None:
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise DatabaseConfigurationError("the V5 database URL must use Postgres")
        self._database_url = database_url
        self._connector = connector

    def deliver(self, message: OutboxMessage) -> DeliveryReceipt:
        if not isinstance(message, OutboxMessage):
            raise ValueError("Teams outbox message is invalid")
        if message.topic == "teams.approval":
            self._deliver_approval(message)
        elif message.topic == "teams.approval-status":
            self._deliver_approval_status(message)
        elif message.topic == "teams.progress":
            self._deliver_progress(message)
        else:
            raise ValueError("Teams notification topic is unsupported")
        return DeliveryReceipt(message.idempotency_key)

    def _deliver_approval_status(self, message: OutboxMessage) -> None:
        payload = _payload(message, "teams.approval-status")
        project_id = _same_project(message, payload)
        gate_id = _identifier(payload["gate_id"], "gate_id")
        status = _identifier(payload["status"], "status")
        self.update_approval(
            project_id=project_id,
            gate_id=gate_id,
            status=status,
        )

    def update_approval(self, *, project_id: str, gate_id: str, status: str) -> None:
        project_id = _identifier(project_id, "project_id")
        gate_id = _identifier(gate_id, "gate_id")
        if status not in _CARD_STATUSES:
            raise ValueError("Teams approval card status is invalid")
        payload, idempotency_key = self._approval_delivery(
            project_id=project_id,
            gate_id=gate_id,
            require_dispatched=True,
        )
        role_id = _identifier(payload["requested_role_id"], "requested_role_id")
        sponsors = _identifiers(payload["sponsor_ids"], "sponsor_ids")
        summary = _summary(payload["summary"])
        expires_at = _timestamp(payload["expires_at"])
        original_card = _approval_card(
            project_id=project_id,
            gate_id=gate_id,
            summary=summary,
            expires_at=expires_at,
        )
        resolved_card = _approval_card(
            project_id=project_id,
            gate_id=gate_id,
            summary=summary,
            expires_at=expires_at,
            status=status,
        )
        for sponsor_id in sponsors:
            operation_id = _operation_id(idempotency_key, sponsor_id)
            delivery = self._connector.send_personal_card(
                project_id=project_id,
                role_id=role_id,
                recipient_id=sponsor_id,
                fallback_text=f"Approval requested: {summary}",
                card=original_card,
                operation_id=operation_id,
            )
            self._connector.update_personal_card(
                project_id=project_id,
                role_id=role_id,
                recipient_id=sponsor_id,
                external_delivery_id=delivery.external_delivery_id,
                fallback_text=f"Approval {status}: {summary}",
                card=resolved_card,
                operation_id=_operation_id(operation_id, status),
            )

    def delivered_approval_authority(
        self,
        *,
        project_id: str,
        gate_id: str,
        sponsor_id: str,
    ) -> Mapping[str, object]:
        project_id = _identifier(project_id, "project_id")
        gate_id = _identifier(gate_id, "gate_id")
        sponsor_id = _identifier(sponsor_id, "sponsor_id")
        payload, _ = self._approval_delivery(
            project_id=project_id,
            gate_id=gate_id,
            require_dispatched=True,
        )
        if sponsor_id not in _identifiers(payload["sponsor_ids"], "sponsor_ids"):
            raise LifecycleAuthorizationError(
                "authenticated sponsor has no delivered card for this gate"
            )
        return payload

    def _deliver_approval(self, message: OutboxMessage) -> None:
        payload = _payload(message, "teams.approval")
        project_id = _same_project(message, payload)
        role_id = _identifier(payload["requested_role_id"], "requested_role_id")
        gate_id = _identifier(payload["gate_id"], "gate_id")
        sponsors = _identifiers(payload["sponsor_ids"], "sponsor_ids")
        summary = _summary(payload["summary"])
        expires_at = _timestamp(payload["expires_at"])
        card = _approval_card(
            project_id=project_id,
            gate_id=gate_id,
            summary=summary,
            expires_at=expires_at,
        )
        for sponsor_id in sponsors:
            self._connector.send_personal_card(
                project_id=project_id,
                role_id=role_id,
                recipient_id=sponsor_id,
                fallback_text=f"Approval requested: {summary}",
                card=card,
                operation_id=_operation_id(message.idempotency_key, sponsor_id),
            )

    def _deliver_progress(self, message: OutboxMessage) -> None:
        payload = _payload(message, "teams.progress")
        project_id = _same_project(message, payload)
        role_id = _identifier(payload["role_id"], "role_id")
        sponsors = _identifiers(payload["sponsor_ids"], "sponsor_ids")
        summary = _summary(payload["safe_summary"])
        status = _short_text(payload["status"], "status", 64)
        next_action = _short_text(payload["next_action"], "next_action", 1000)
        reject_sensitive_content(next_action, "Teams progress next action")
        text = f"{summary}\n\nStatus: {status}\nNext action: {next_action}"
        for sponsor_id in sponsors:
            self._connector.send_personal_message(
                project_id=project_id,
                role_id=role_id,
                recipient_id=sponsor_id,
                text=text,
                operation_id=_operation_id(message.idempotency_key, sponsor_id),
            )

    def _approval_delivery(
        self,
        *,
        project_id: str,
        gate_id: str,
        require_dispatched: bool,
    ) -> tuple[Mapping[str, object], str]:
        dispatched = "AND dispatched_at IS NOT NULL" if require_dispatched else ""
        try:
            with psycopg.connect(
                self._database_url,
                autocommit=True,
                row_factory=dict_row,
            ) as connection:
                row = connection.execute(
                    f"""
                    SELECT payload, idempotency_key
                    FROM {SCHEMA}.outbox
                    WHERE project_id = %s AND topic = 'teams.approval'
                      AND payload ->> 'gate_id' = %s
                      {dispatched}
                    ORDER BY outbox_id DESC LIMIT 1
                    """,
                    (project_id, gate_id),
                ).fetchone()
        except Exception:
            raise TeamsNotificationError(
                "Teams approval delivery lookup failed"
            ) from None
        if row is None or not isinstance(row["payload"], Mapping):
            raise LifecycleNotFound("delivered Teams approval card not found")
        return row["payload"], _identifier(row["idempotency_key"], "idempotency_key")


class TeamsApprovalCallbackHandler:
    def __init__(
        self,
        database_url: str,
        *,
        approvals: SponsorApprovalCoordinator,
        notifications: TeamsNotificationAdapter,
    ) -> None:
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise DatabaseConfigurationError("the V5 database URL must use Postgres")
        self._database_url = database_url
        self._approvals = approvals
        self._notifications = notifications

    def handle_action(
        self,
        *,
        action: Mapping[str, object],
        authenticated_sponsor_id: str,
        activity_id: str,
    ) -> TeamsApprovalCallbackResult:
        if not isinstance(action, Mapping) or set(action) != {
            "action",
            "schema_version",
            "project_id",
            "gate_id",
            "decision",
            "rationale",
        }:
            raise ValueError("Teams approval action is invalid")
        if action["action"] != "sponsor_decision" or action["schema_version"] != 1:
            raise ValueError("Teams approval action is invalid")
        return self.handle(
            project_id=action["project_id"],
            gate_id=action["gate_id"],
            authenticated_sponsor_id=authenticated_sponsor_id,
            decision=action["decision"],
            rationale=action["rationale"],
            activity_id=activity_id,
        )

    def handle(
        self,
        *,
        project_id: str,
        gate_id: str,
        authenticated_sponsor_id: str,
        decision: str,
        rationale: str,
        activity_id: str,
    ) -> TeamsApprovalCallbackResult:
        project_id = _identifier(project_id, "project_id")
        gate_id = _identifier(gate_id, "gate_id")
        sponsor_id = _identifier(authenticated_sponsor_id, "sponsor_id")
        activity_id = _identifier(activity_id, "activity_id")
        if decision not in {"approved", "rejected"}:
            raise ValueError("Teams approval decision is invalid")
        rationale = _short_text(rationale, "rationale", 4000)
        self._notifications.delivered_approval_authority(
            project_id=project_id,
            gate_id=gate_id,
            sponsor_id=sponsor_id,
        )
        if self._expired(project_id, gate_id):
            self._notifications.update_approval(
                project_id=project_id,
                gate_id=gate_id,
                status="expired",
            )
            return TeamsApprovalCallbackResult(
                project_id, gate_id, sponsor_id, "expired", None
            )
        approval = self._approvals.decide(
            project_id=project_id,
            gate_id=gate_id,
            sponsor_id=sponsor_id,
            decision=decision,
            rationale=rationale,
            evidence={"channel": "teams", "activity_id": activity_id},
            operation_id=_operation_id("callback", activity_id),
        )
        return TeamsApprovalCallbackResult(
            project_id,
            gate_id,
            sponsor_id,
            decision,
            approval.approval_id,
        )

    def _expired(self, project_id: str, gate_id: str) -> bool:
        try:
            with psycopg.connect(self._database_url, autocommit=True) as connection:
                row = connection.execute(
                    f"""
                    SELECT status, expires_at <= clock_timestamp()
                    FROM {SCHEMA}.gates
                    WHERE project_id = %s AND gate_id = %s
                    """,
                    (project_id, gate_id),
                ).fetchone()
        except Exception:
            raise TeamsNotificationError("Teams approval expiry check failed") from None
        if row is None:
            raise LifecycleNotFound("managed sponsor gate not found")
        return row[0] == "pending" and row[1] is True


class TeamsProgressPublisher:
    def __init__(self, database_url: str) -> None:
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise DatabaseConfigurationError("the V5 database URL must use Postgres")
        self._events = EventStore(database_url)

    def publish(
        self,
        *,
        project_id: str,
        checkpoint_id: str,
        operation_id: str,
    ) -> StoredEvent:
        project_id = _identifier(project_id, "project_id")
        checkpoint_id = _identifier(checkpoint_id, "checkpoint_id")
        operation_id = _identifier(operation_id, "operation_id")
        with self._events.transaction() as transaction:
            transaction.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"teams-progress:{project_id}:{checkpoint_id}",),
            )
            existing = transaction.execute(
                f"""
                SELECT event_id, project_id, work_item_id, actor_id,
                       correlation_id, causation_id, aggregate_type,
                       aggregate_id, event_type, payload, occurred_at::text
                FROM {SCHEMA}.events
                WHERE project_id = %s AND aggregate_type = 'teams-progress'
                  AND aggregate_id = %s
                """,
                (project_id, checkpoint_id),
            ).fetchone()
            if existing is not None:
                return StoredEvent(*existing)
            row = transaction.execute(
                f"""
                SELECT progress.work_item_id, instance.role_id,
                       progress.status, progress.next_action,
                       progress.safe_summary
                FROM {SCHEMA}.progress AS progress
                JOIN {SCHEMA}.role_instances AS instance
                  ON instance.project_id = progress.project_id
                 AND instance.instance_id = progress.role_instance_id
                WHERE progress.project_id = %s AND progress.checkpoint_id = %s
                """,
                (project_id, checkpoint_id),
            ).fetchone()
            if row is None:
                raise LifecycleNotFound("progress checkpoint not found")
            sponsors = tuple(
                item[0]
                for item in transaction.execute(
                    f"""
                    SELECT sponsor_id FROM {SCHEMA}.project_sponsors
                    WHERE project_id = %s ORDER BY sponsor_id
                    """,
                    (project_id,),
                ).fetchall()
            )
            if not sponsors:
                raise LifecycleAuthorizationError(
                    "project has no sponsors for Teams progress"
                )
            payload = {
                "project_id": project_id,
                "work_item_id": row[0],
                "checkpoint_id": checkpoint_id,
                "role_id": row[1],
                "sponsor_ids": list(sponsors),
                "status": row[2],
                "next_action": row[3],
                "safe_summary": row[4],
            }
            return transaction.append(
                EventDraft(
                    project_id=project_id,
                    work_item_id=row[0],
                    actor_id=row[1],
                    correlation_id=operation_id,
                    aggregate_type="teams-progress",
                    aggregate_id=checkpoint_id,
                    event_type="teams.progress_requested",
                    payload=payload,
                ),
                (OutboundDraft(topic="teams.progress", payload=payload),),
            )


def _payload(message: OutboxMessage, topic: str) -> Mapping[str, object]:
    if message.topic != topic or not isinstance(message.payload, Mapping):
        raise ValueError("Teams notification payload is invalid")
    return message.payload


def _same_project(message: OutboxMessage, payload: Mapping[str, object]) -> str:
    project_id = _identifier(payload.get("project_id"), "project_id")
    if project_id != message.project_id:
        raise ValueError("Teams notification project authority is invalid")
    return project_id


def _approval_card(
    *,
    project_id: str,
    gate_id: str,
    summary: str,
    expires_at: str,
    status: str | None = None,
) -> dict[str, object]:
    body: list[dict[str, object]] = [
        {
            "type": "TextBlock",
            "text": "Sponsor approval required" if status is None else f"Approval {status}",
            "weight": "Bolder",
            "size": "Medium",
        },
        {"type": "TextBlock", "text": summary, "wrap": True},
        {
            "type": "FactSet",
            "facts": [
                {"title": "Project", "value": project_id},
                {"title": "Gate", "value": gate_id},
                {"title": "Expires", "value": expires_at},
            ],
        },
    ]
    actions: list[dict[str, object]] = []
    if status is None:
        body.append(
            {
                "type": "Input.Text",
                "id": "rationale",
                "label": "Rationale",
                "isMultiline": True,
                "isRequired": True,
                "maxLength": 4000,
            }
        )
        action_data = {
            "action": "sponsor_decision",
            "schema_version": 1,
            "project_id": project_id,
            "gate_id": gate_id,
        }
        actions = [
            {
                "type": "Action.Submit",
                "title": "Approve",
                "style": "positive",
                "data": {**action_data, "decision": "approved"},
            },
            {
                "type": "Action.Submit",
                "title": "Reject",
                "style": "destructive",
                "data": {**action_data, "decision": "rejected"},
            },
        ]
    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.5",
        "body": body,
        "actions": actions,
    }


def _identifiers(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"{field} is invalid")
    items = tuple(_identifier(item, field) for item in value)
    if not items or len(items) != len(set(items)):
        raise ValueError(f"{field} is invalid")
    return items


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{field} is invalid")
    return value


def _summary(value: object) -> str:
    summary = _short_text(value, "summary", 1000)
    reject_sensitive_content(summary, "Teams notification summary")
    return summary


def _short_text(value: object, field: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or "\x00" in value
    ):
        raise ValueError(f"{field} is invalid")
    return value.strip()


def _timestamp(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Teams notification timestamp is invalid")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Teams notification timestamp is invalid")
    return parsed.isoformat()


def _operation_id(*values: str) -> str:
    digest = hashlib.sha256("\x00".join(values).encode("utf-8")).hexdigest()
    return f"teams:{digest}"
