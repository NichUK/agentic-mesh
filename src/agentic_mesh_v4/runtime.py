from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from agentic_mesh_v4.codex_protocol import CodexAppServerClient
from agentic_mesh_v4.codex_protocol import CodexProtocolError
from agentic_mesh_v4.config import V4ProjectConfig
from agentic_mesh_v4.db import V4Database
from agentic_mesh_v4.db import utc_now
from agentic_mesh_v4.teams_delivery import TeamsReplySender


class ClientFactory(Protocol):
    def __call__(self, role_id: str) -> CodexAppServerClient:
        ...


@dataclass(frozen=True)
class DispatchResult:
    message_id: str
    state: str
    thread_id: str | None = None
    turn_id: str | None = None
    error: str | None = None


class V4Runtime:
    def __init__(
        self,
        *,
        db: V4Database,
        project_config: V4ProjectConfig,
        client_factory: ClientFactory | None = None,
        teams_reply_sender: TeamsReplySender | None = None,
    ) -> None:
        self.db = db
        self.project_config = project_config
        self.client_factory = client_factory
        self.teams_reply_sender = teams_reply_sender

    def register_roles(self) -> None:
        for role in self.project_config.roles:
            self.db.upsert_role_instance(
                role_instance_id=f"{self.project_config.project_id}.{role.role_id}.1",
                role_id=role.role_id,
                display_name=role.display_name,
                service_name=role.service_name,
                authority=role.authority,
                codex_endpoint=f"ws://{role.service_name}:{role.codex_port}",
            )

    def enqueue_conversation(
        self,
        *,
        target_role: str,
        text: str,
        source: str = "teams",
        conversation_ref: str | None = None,
        thread_ref: str | None = None,
        steering: bool = False,
        payload: dict[str, object] | None = None,
    ) -> str:
        self.project_config.role(target_role)
        return self.db.enqueue_message(
            target_role=target_role,
            text=text,
            source=source,
            conversation_ref=conversation_ref,
            thread_ref=thread_ref,
            steering=steering,
            payload=dict(payload or {}),
        )

    def enqueue_or_steer_conversation(
        self,
        *,
        target_role: str,
        text: str,
        source: str = "teams",
        conversation_ref: str | None = None,
        thread_ref: str | None = None,
        steering: bool = False,
        payload: dict[str, object] | None = None,
    ) -> str:
        role = self.project_config.role(target_role)
        active = self.db.active_message_for_role(
            target_role=target_role,
            conversation_ref=conversation_ref,
        )
        force_queue = _starts_with_queue_directive(text)
        should_steer = (steering or (active is not None and bool(conversation_ref))) and not force_queue
        message_id = self.db.enqueue_message(
            target_role=target_role,
            text=text,
            source=source,
            conversation_ref=conversation_ref,
            thread_ref=thread_ref,
            steering=should_steer,
            payload=dict(payload or {}),
        )
        if not should_steer or self.client_factory is None:
            return message_id
        role_instance_id = f"{self.project_config.project_id}.{role.role_id}.1"
        thread_id = self._active_thread_id(role_instance_id)
        if not thread_id:
            return message_id
        try:
            client = self.client_factory(role.role_id)
            if not client.initialized:
                client.initialize()
            client.resume_thread(thread_id)
            client.steer_turn(thread_id=thread_id, text=text)
        except Exception as exc:
            self.db.mark_message_state(
                message_id,
                state="queued",
                summary=f"Steering failed; queued for normal delivery: {exc}",
            )
            return message_id
        self.db.mark_message_state(
            message_id,
            state="steered",
            summary=f"Steered into active turn for {role_instance_id}",
        )
        return message_id

    def dispatch_once(self, *, role_id: str) -> DispatchResult | None:
        role = self.project_config.role(role_id)
        role_instance_id = f"{self.project_config.project_id}.{role.role_id}.1"
        message = self.db.claim_next_message(role_id=role.role_id, worker_id=role_instance_id)
        if message is None:
            return None
        if self.client_factory is None:
            self.db.mark_message_state(
                message.message_id,
                state="failed",
                summary="No Codex app-server client factory is configured.",
            )
            return DispatchResult(message_id=message.message_id, state="failed", error="missing client factory")
        try:
            client = self.client_factory(role.role_id)
            if not client.initialized:
                client.initialize()
            thread_id = self._thread_for_role(client=client, role_instance_id=role_instance_id, role=role)
            if message.steering:
                client.steer_turn(thread_id=thread_id, text=message.text)
                turn_id = None
                state = "steered"
            else:
                turn_id = client.start_turn(thread_id=thread_id, text=message.text, model=role.model)
                state = "active_turn"
                now = utc_now()
                with self.db.connection:
                    self.db.connection.execute(
                        """
                        UPDATE role_instances
                        SET active_turn_id=?, state='active', updated_at=?
                        WHERE role_instance_id=?
                        """,
                        (turn_id, now, role_instance_id),
                    )
                    if turn_id is not None:
                        self.db.connection.execute(
                            """
                            INSERT OR REPLACE INTO codex_turns(
                              turn_id, thread_id, message_id, status, started_at, completed_at
                            ) VALUES(?,?,?,?,?,NULL)
                            """,
                            (turn_id, thread_id, message.message_id, "active", now),
                        )
            self.db.mark_message_state(message.message_id, state=state, summary=f"Delivered to {role_instance_id}")
            reply_text = self._drain_available_events(
                client=client,
                role_instance_id=role_instance_id,
                approval_policy=getattr(role, "approval_policy"),
                thread_id=thread_id,
                turn_id=turn_id,
                message_id=message.message_id,
            )
            if message.source == "teams" and reply_text.strip():
                sender = self.teams_reply_sender or TeamsReplySender.from_env()
                delivery_id = sender.send_reply(
                    role_id=role.role_id,
                    activity=message.payload,
                    text_markdown=reply_text.strip(),
                )
                self.db.record_message_journal(
                    message_id=message.message_id,
                    correlation_id=message.correlation_id,
                    stage="reply_delivered",
                    status="delivered",
                    summary=f"Delivered Teams reply {delivery_id}",
                    role_instance_id=role_instance_id,
                )
            now = utc_now()
            with self.db.connection:
                self.db.connection.execute(
                    """
                    UPDATE role_instances
                    SET active_turn_id=NULL, state='ready', updated_at=?
                    WHERE role_instance_id=?
                    """,
                    (now, role_instance_id),
                )
                if turn_id is not None:
                    self.db.connection.execute(
                        """
                        UPDATE codex_turns
                        SET status='completed', completed_at=?
                        WHERE turn_id=?
                        """,
                        (now, turn_id),
                    )
            self.db.mark_message_state(message.message_id, state="completed", summary=f"Completed delivery to {role_instance_id}")
            return DispatchResult(message_id=message.message_id, state="completed", thread_id=thread_id, turn_id=turn_id)
        except Exception as exc:
            with self.db.connection:
                self.db.connection.execute(
                    """
                    UPDATE role_instances
                    SET active_turn_id=NULL, state='ready', updated_at=?
                    WHERE role_instance_id=?
                    """,
                    (utc_now(), role_instance_id),
                )
            if _looks_like_agent_unavailable(exc):
                self.db.mark_message_state(
                    message.message_id,
                    state="queued",
                    summary=f"Agent app-server unavailable; queued for retry: {exc}",
                )
                return DispatchResult(message_id=message.message_id, state="queued", error=str(exc))
            self.db.mark_message_state(message.message_id, state="failed", summary=str(exc))
            return DispatchResult(message_id=message.message_id, state="failed", error=str(exc))

    def _thread_for_role(self, *, client: CodexAppServerClient, role_instance_id: str, role: object) -> str:
        snapshot = self.db.snapshot()
        for item in snapshot["roles"]:
            if item["role_instance_id"] == role_instance_id and item.get("active_thread_id"):
                thread_id = str(item["active_thread_id"])
                if self._thread_matches_role(thread_id=thread_id, role_instance_id=role_instance_id, role=role):
                    client.resume_thread(thread_id)
                    return thread_id
                self._retire_thread(role_instance_id=role_instance_id, thread_id=thread_id)
        thread_id = client.start_thread(
            model=getattr(role, "model"),
            sandbox_mode=getattr(role, "sandbox_mode"),
            approval_policy=getattr(role, "approval_policy"),
        )
        now = utc_now()
        with self.db.connection:
            self.db.connection.execute(
                """
                UPDATE role_instances
                SET active_thread_id=?, state='ready', updated_at=?
                WHERE role_instance_id=?
                """,
                (thread_id, now, role_instance_id),
            )
            self.db.connection.execute(
                """
                INSERT OR IGNORE INTO codex_threads(
                  thread_id, role_instance_id, sandbox_mode, approval_policy, status, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    thread_id,
                    role_instance_id,
                    getattr(role, "sandbox_mode"),
                    getattr(role, "approval_policy"),
                    "active",
                    now,
                    now,
                ),
            )
        return thread_id

    def _active_thread_id(self, role_instance_id: str) -> str | None:
        row = self.db.connection.execute(
            """
            SELECT active_thread_id
            FROM role_instances
            WHERE role_instance_id=?
            """,
            (role_instance_id,),
        ).fetchone()
        if row is None or row["active_thread_id"] is None:
            return None
        return str(row["active_thread_id"])

    def _thread_matches_role(self, *, thread_id: str, role_instance_id: str, role: object) -> bool:
        row = self.db.connection.execute(
            """
            SELECT sandbox_mode, approval_policy, status
            FROM codex_threads
            WHERE thread_id=? AND role_instance_id=?
            """,
            (thread_id, role_instance_id),
        ).fetchone()
        if row is None:
            return False
        return (
            str(row["status"]) == "active"
            and row["sandbox_mode"] == getattr(role, "sandbox_mode")
            and row["approval_policy"] == getattr(role, "approval_policy")
        )

    def _retire_thread(self, *, role_instance_id: str, thread_id: str) -> None:
        now = utc_now()
        with self.db.connection:
            self.db.connection.execute(
                "UPDATE codex_threads SET status='retired', updated_at=? WHERE thread_id=?",
                (now, thread_id),
            )
            self.db.connection.execute(
                """
                UPDATE role_instances
                SET active_thread_id=NULL, active_turn_id=NULL, updated_at=?
                WHERE role_instance_id=? AND active_thread_id=?
                """,
                (now, role_instance_id, thread_id),
            )

    def _drain_available_events(
        self,
        *,
        client: CodexAppServerClient,
        role_instance_id: str,
        approval_policy: str,
        thread_id: str,
        turn_id: str | None,
        message_id: str,
    ) -> str:
        reply_parts: list[str] = []
        while True:
            event = client.receive_event()
            if event is None:
                return "".join(reply_parts)
            method = str(event.get("method") or "unknown")
            params = event.get("params") if isinstance(event.get("params"), dict) else {}
            content = _event_content(method, params)
            if method == "item/agentMessage/delta":
                reply_parts.append(content)
            if _should_auto_accept_server_request(event=event, approval_policy=approval_policy):
                client.respond_to_server_request(request_id=event["id"], result={"decision": "accept"})
                self.db.record_agent_event(
                    role_instance_id=role_instance_id,
                    event_type=f"{method}/autoAccepted",
                    content="Auto-accepted server approval request for approval_policy=never.",
                    payload={"request_id": event["id"], "method": method},
                    thread_id=thread_id,
                    turn_id=turn_id,
                    message_id=message_id,
                )
            self.db.record_agent_event(
                role_instance_id=role_instance_id,
                event_type=method,
                content=content,
                payload=event,
                thread_id=thread_id,
                turn_id=turn_id,
                message_id=message_id,
            )
            if method == "turn/completed":
                return "".join(reply_parts)


def _event_content(method: str, params: dict[str, object]) -> str:
    for key in ("text", "delta", "message", "summary"):
        value = params.get(key)
        if isinstance(value, str):
            return value
    return method


def _should_auto_accept_server_request(*, event: dict[str, object], approval_policy: str) -> bool:
    if approval_policy != "never" or "id" not in event:
        return False
    method = str(event.get("method") or "")
    return method in {
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
        "item/permissions/requestApproval",
    }


def _looks_like_agent_unavailable(exc: BaseException) -> bool:
    text = str(exc).casefold()
    return any(
        marker in text
        for marker in (
            "connection refused",
            "name or service not known",
            "temporary failure in name resolution",
            "timed out",
            "connection reset",
            "websocket",
        )
    )


def _starts_with_queue_directive(text: str) -> bool:
    return re.match(r"^\s*queue\s*:", text, flags=re.IGNORECASE) is not None
