from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from typing import Callable
from typing import Protocol

from agentic_mesh_v4.codex_protocol import CodexAppServerClient
from agentic_mesh_v4.codex_protocol import CodexProtocolError
from agentic_mesh_v4.config import V4ProjectConfig
from agentic_mesh_v4.db import V4Database
from agentic_mesh_v4.db import utc_now
from agentic_mesh_v4.teams_delivery import PROCESSING_REACTION_GLYPH
from agentic_mesh_v4.teams_delivery import PROCESSING_REACTION_NAME
from agentic_mesh_v4.teams_delivery import PROCESSING_REACTION_UNSUPPORTED_REASON
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


class AgentTurnStillRunning(RuntimeError):
    """Raised when a started Codex turn is still running after an event read timeout."""


class V4Runtime:
    def __init__(
        self,
        *,
        db: V4Database,
        project_config: V4ProjectConfig,
        client_factory: ClientFactory | None = None,
        teams_reply_sender: TeamsReplySender | None = None,
        document_syncer: Callable[[], object] | None = None,
        agent_config_root: str | Path | None = None,
    ) -> None:
        self.db = db
        self.project_config = project_config
        self.client_factory = client_factory
        self.teams_reply_sender = teams_reply_sender
        self.document_syncer = document_syncer
        self.agent_config_root = Path(agent_config_root) if agent_config_root is not None else None

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
        if active is None and conversation_ref:
            active = self.db.active_message_for_role(target_role=target_role, unscoped_only=True)
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
        expected_turn_id = self._active_turn_id(role_instance_id)
        try:
            client = self.client_factory(role.role_id)
            if not client.initialized:
                client.initialize()
            self._acknowledge_message_processing_payload(
                source=source,
                payload=dict(payload or {}),
                message_id=message_id,
                correlation_id=f"corr-{message_id}",
                role_id=role.role_id,
                role_instance_id=role_instance_id,
            )
            client.resume_thread(thread_id)
            client.steer_turn(thread_id=thread_id, text=text, expected_turn_id=expected_turn_id)
        except Exception as exc:
            self.db.downgrade_message_to_normal_delivery(
                message_id,
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
        active = self.db.active_message_for_role(target_role=role.role_id)
        if active is not None:
            return None
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
            self._acknowledge_message_processing(
                message=message,
                role_id=role.role_id,
                role_instance_id=role_instance_id,
            )
            thread_id = self._thread_for_role(client=client, role_instance_id=role_instance_id, role=role)
            if message.steering:
                expected_turn_id = self._active_turn_id(role_instance_id)
                if expected_turn_id:
                    client.steer_turn(thread_id=thread_id, text=message.text, expected_turn_id=expected_turn_id)
                    turn_id = None
                    state = "steered"
                else:
                    self.db.downgrade_message_to_normal_delivery(
                        message.message_id,
                        summary="Steering message had no active turn; starting it as a normal turn.",
                    )
                    message = type(message)(
                        message_id=message.message_id,
                        source=message.source,
                        target_role=message.target_role,
                        state=message.state,
                        text=message.text,
                        payload=message.payload,
                        steering=False,
                        correlation_id=message.correlation_id,
                        conversation_ref=message.conversation_ref,
                        thread_ref=message.thread_ref,
                        delivery_attempts=message.delivery_attempts,
                    )
            if not message.steering:
                turn_id = client.start_turn(thread_id=thread_id, text=message.text, model=role.model)
                state = "active_turn"
                now = utc_now()
                with self.db.connection:
                    self.db.connection.execute(
                        """
                        UPDATE codex_turns
                        SET status='stale_closed', completed_at=?
                        WHERE thread_id=? AND status='active' AND turn_id<>?
                        """,
                        (now, thread_id, turn_id),
                    )
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
                            INSERT INTO codex_turns(
                              turn_id, thread_id, message_id, status, started_at, completed_at
                            ) VALUES(?,?,?,?,?,NULL)
                            ON CONFLICT(turn_id) DO UPDATE SET
                              thread_id=excluded.thread_id,
                              message_id=excluded.message_id,
                              status=excluded.status,
                              started_at=excluded.started_at,
                              completed_at=NULL
                            """,
                            (turn_id, thread_id, message.message_id, "active", now),
                        )
            self.db.mark_message_state(message.message_id, state=state, summary=f"Delivered to {role_instance_id}")
            teams_sender: object | None = None
            if message.source == "teams":
                teams_sender = self.teams_reply_sender or TeamsReplySender.from_env()
            reply_text = self._drain_available_events(
                client=client,
                role_instance_id=role_instance_id,
                role_id=role.role_id,
                approval_policy=getattr(role, "approval_policy"),
                thread_id=thread_id,
                turn_id=turn_id,
                message_id=message.message_id,
                message_source=message.source,
                message_payload=message.payload,
                message_correlation_id=message.correlation_id,
                teams_reply_sender=teams_sender,
            )
            if message.source == "teams" and reply_text.strip():
                assert teams_sender is not None
                delivery_id = teams_sender.send_reply(
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
            self._sync_documents_after_turn(message_id=message.message_id, correlation_id=message.correlation_id, role_instance_id=role_instance_id)
            return DispatchResult(message_id=message.message_id, state="completed", thread_id=thread_id, turn_id=turn_id)
        except AgentTurnStillRunning as exc:
            self.db.mark_message_state(
                message.message_id,
                state="active_turn",
                summary=f"Agent turn is still running for {role_instance_id}: {exc}",
            )
            return DispatchResult(
                message_id=message.message_id,
                state="active_turn",
                thread_id=self._active_thread_id(role_instance_id),
                turn_id=self._active_turn_id(role_instance_id),
                error=str(exc),
            )
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

    def _acknowledge_message_processing(
        self,
        *,
        message: object,
        role_id: str,
        role_instance_id: str,
    ) -> None:
        if getattr(message, "source") != "teams":
            return
        payload = getattr(message, "payload")
        if not isinstance(payload, dict):
            return
        self._acknowledge_message_processing_payload(
            source=getattr(message, "source"),
            payload=payload,
            message_id=getattr(message, "message_id"),
            correlation_id=getattr(message, "correlation_id"),
            role_id=role_id,
            role_instance_id=role_instance_id,
        )

    def _acknowledge_message_processing_payload(
        self,
        *,
        source: str,
        payload: dict[str, object],
        message_id: str,
        correlation_id: str,
        role_id: str,
        role_instance_id: str,
    ) -> None:
        if source != "teams":
            return
        sender = self.teams_reply_sender or TeamsReplySender.from_env()
        reaction = getattr(sender, "add_processing_reaction", None)
        if not callable(reaction):
            self.db.record_message_journal(
                message_id=message_id,
                correlation_id=correlation_id,
                role_instance_id=role_instance_id,
                stage="processing_reaction",
                status="unsupported",
                summary="Processing reaction skipped: Teams sender does not support reactions.",
            )
            return
        try:
            delivery_id = reaction(
                role_id=role_id,
                activity=payload,
            )
        except Exception as exc:  # noqa: BLE001 - acknowledgement must not block processing.
            self.db.record_message_journal(
                message_id=message_id,
                correlation_id=correlation_id,
                role_instance_id=role_instance_id,
                stage="processing_reaction",
                status="failed",
                summary=f"Processing reaction failed: {exc}",
            )
            self.db.record_agent_event(
                role_instance_id=role_instance_id,
                event_type="processing_reaction/failed",
                content=str(exc),
                payload={"error": str(exc), "reaction": PROCESSING_REACTION_NAME},
                message_id=message_id,
            )
            return
        if delivery_id is None:
            self.db.record_message_journal(
                message_id=message_id,
                correlation_id=correlation_id,
                role_instance_id=role_instance_id,
                stage="processing_reaction",
                status="unsupported",
                summary=f"Processing reaction skipped: {PROCESSING_REACTION_UNSUPPORTED_REASON}.",
            )
            return
        self.db.record_message_journal(
            message_id=message_id,
            correlation_id=correlation_id,
            role_instance_id=role_instance_id,
            stage="processing_reaction",
            status="delivered",
            summary=f"Delivered Teams processing reaction {PROCESSING_REACTION_NAME} {PROCESSING_REACTION_GLYPH}: {delivery_id}",
        )

    def _sync_documents_after_turn(self, *, message_id: str, correlation_id: str, role_instance_id: str) -> None:
        if self.document_syncer is None:
            return
        try:
            result = self.document_syncer()
        except Exception as exc:  # noqa: BLE001 - sync must not fail the completed agent turn.
            self.db.record_message_journal(
                message_id=message_id,
                correlation_id=correlation_id,
                role_instance_id=role_instance_id,
                stage="document_sync",
                status="failed",
                summary=f"Document sync failed after agent turn: {exc}",
            )
            self.db.record_agent_event(
                role_instance_id=role_instance_id,
                event_type="document_sync/failed",
                content=str(exc),
                message_id=message_id,
            )
            return
        uploaded = getattr(result, "uploaded", None)
        folders_created = getattr(result, "folders_created", None)
        root_path = getattr(result, "root_path", "")
        summary = "Document sync completed"
        if uploaded is not None and folders_created is not None:
            summary = f"Document sync completed: uploaded {uploaded}, folders created {folders_created}, root {root_path}"
        self.db.record_message_journal(
            message_id=message_id,
            correlation_id=correlation_id,
            role_instance_id=role_instance_id,
            stage="document_sync",
            status="completed",
            summary=summary,
        )

    def _thread_for_role(self, *, client: CodexAppServerClient, role_instance_id: str, role: object) -> str:
        agent_config_hash = self._agent_config_hash(role_id=getattr(role, "role_id"))
        snapshot = self.db.snapshot()
        for item in snapshot["roles"]:
            if item["role_instance_id"] == role_instance_id and item.get("active_thread_id"):
                thread_id = str(item["active_thread_id"])
                if self._thread_matches_role(
                    thread_id=thread_id,
                    role_instance_id=role_instance_id,
                    role=role,
                    agent_config_hash=agent_config_hash,
                ):
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
                INSERT INTO codex_threads(
                  thread_id, role_instance_id, agent_config_hash, sandbox_mode, approval_policy, status, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(thread_id) DO NOTHING
                """,
                (
                    thread_id,
                    role_instance_id,
                    agent_config_hash,
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

    def _active_turn_id(self, role_instance_id: str) -> str | None:
        row = self.db.connection.execute(
            """
            SELECT active_turn_id
            FROM role_instances
            WHERE role_instance_id=?
            """,
            (role_instance_id,),
        ).fetchone()
        if row is None or row["active_turn_id"] is None:
            return None
        return str(row["active_turn_id"])

    def _thread_matches_role(
        self,
        *,
        thread_id: str,
        role_instance_id: str,
        role: object,
        agent_config_hash: str,
    ) -> bool:
        row = self.db.connection.execute(
            """
            SELECT sandbox_mode, approval_policy, agent_config_hash, status
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
            and (row["agent_config_hash"] or "") == agent_config_hash
        )

    def _agent_config_hash(self, *, role_id: str) -> str:
        digest = hashlib.sha256()
        if self.agent_config_root is not None:
            role_root = self.agent_config_root / role_id / "1"
            for filename in ("AGENTS.md", "container.json"):
                path = role_root / filename
                if path.exists():
                    digest.update(filename.encode("utf-8"))
                    digest.update(b"\0")
                    digest.update(path.read_bytes())
                    digest.update(b"\0")
            if digest.digest() != hashlib.sha256().digest():
                return digest.hexdigest()
        role = self.project_config.role(role_id)
        digest.update(role.role_id.encode("utf-8"))
        digest.update(str(role.sandbox_mode).encode("utf-8"))
        digest.update(str(role.approval_policy).encode("utf-8"))
        digest.update(str(role.model).encode("utf-8"))
        digest.update(str(role.reasoning_effort).encode("utf-8"))
        return digest.hexdigest()

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
        role_id: str,
        approval_policy: str,
        thread_id: str,
        turn_id: str | None,
        message_id: str,
        message_source: str,
        message_payload: dict[str, Any],
        message_correlation_id: str,
        teams_reply_sender: object | None,
    ) -> str:
        fallback_reply_parts: list[str] = []
        final_reply: str | None = None
        while True:
            try:
                event = client.receive_event()
            except Exception as exc:
                if _looks_like_receive_timeout(exc):
                    event_type = "turn/readTimeoutAfterOutput" if fallback_reply_parts or final_reply else "turn/readTimeoutStillRunning"
                    resolved_turn_id = turn_id or self._active_turn_id(role_instance_id)
                    self.db.record_agent_event(
                        role_instance_id=role_instance_id,
                        event_type=event_type,
                        content=str(exc),
                        payload={"error": str(exc)},
                        thread_id=thread_id,
                        turn_id=resolved_turn_id,
                        message_id=message_id,
                    )
                    raise AgentTurnStillRunning(
                        f"no app-server event before read timeout; leaving turn {resolved_turn_id or '<unknown>'} active"
                    ) from exc
                raise
            if event is None:
                return final_reply or "".join(fallback_reply_parts)
            method = str(event.get("method") or "unknown")
            params = event.get("params") if isinstance(event.get("params"), dict) else {}
            event_thread_id = params.get("threadId")
            event_turn_id = params.get("turnId")
            if (
                turn_id is not None
                and isinstance(event_turn_id, str)
                and event_turn_id
                and event_turn_id != turn_id
            ):
                self.db.record_agent_event(
                    role_instance_id=role_instance_id,
                    event_type=f"{method}/foreignTurnIgnored",
                    content=_event_content(method, params),
                    payload=event,
                    thread_id=event_thread_id if isinstance(event_thread_id, str) else thread_id,
                    turn_id=event_turn_id,
                    message_id=self._message_id_for_turn(event_turn_id),
                )
                continue
            content = _event_content(method, params)
            if method == "item/agentMessage/delta":
                fallback_reply_parts.append(content)
            completed_message = _completed_agent_message(event)
            if completed_message is not None:
                phase, text = completed_message
                if phase == "final_answer":
                    final_reply = text
                elif (
                    phase == "commentary"
                    and message_source == "teams"
                    and text.strip()
                    and teams_reply_sender is not None
                ):
                    delivery_id = teams_reply_sender.send_reply(
                        role_id=role_id,
                        activity=message_payload,
                        text_markdown=text.strip(),
                    )
                    self.db.record_message_journal(
                        message_id=message_id,
                        correlation_id=message_correlation_id,
                        stage="progress_delivered",
                        status="delivered",
                        summary=f"Delivered Teams progress reply {delivery_id}",
                        role_instance_id=role_instance_id,
                    )
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
                return final_reply or "".join(fallback_reply_parts)

    def _message_id_for_turn(self, turn_id: str) -> str | None:
        row = self.db.connection.execute(
            "SELECT message_id FROM codex_turns WHERE turn_id=?",
            (turn_id,),
        ).fetchone()
        if row is None:
            return None
        return str(row["message_id"])


def _event_content(method: str, params: dict[str, object]) -> str:
    for key in ("text", "delta", "message", "summary"):
        value = params.get(key)
        if isinstance(value, str):
            return value
    return method


def _completed_agent_message(event: dict[str, object]) -> tuple[str, str] | None:
    if event.get("method") != "item/completed":
        return None
    params = event.get("params")
    if not isinstance(params, dict):
        return None
    item = params.get("item")
    if not isinstance(item, dict) or item.get("type") != "agentMessage":
        return None
    text = item.get("text")
    if not isinstance(text, str):
        return None
    phase = item.get("phase")
    return (phase if isinstance(phase, str) else "", text)


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


def _looks_like_receive_timeout(exc: BaseException) -> bool:
    text = str(exc).casefold()
    return isinstance(exc, TimeoutError) or "timed out" in text or "timeout" in text


def _starts_with_queue_directive(text: str) -> bool:
    return re.match(r"^\s*queue\s*:", text, flags=re.IGNORECASE) is not None
