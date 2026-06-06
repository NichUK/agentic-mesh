from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentic_mesh.journal import EventJournal
from agentic_mesh.models import utc_now_iso
from agentic_mesh.notifications import redacted_error_class
from agentic_mesh.notifications import safe_filename
from agentic_mesh.notifications import validate_logical_id


THREADED_CONTEXT_SCHEMA_VERSION = "threaded-context-v0"
THREAD_ROUTE_SCHEMA_VERSION = "thread-route-v0"
SUPPORT_ACCESS_SCHEMA_VERSION = "threaded-context-support-access-v0"
RECOVERY_SCHEMA_VERSION = "threaded-context-recovery-v0"

MESSAGE_TYPE_THREADED_CONTEXT_ATTENTION_REQUESTED = (
    "threaded_context.attention_requested"
)

TERMINAL_LIFECYCLE_STATES = {
    "completed",
    "closed",
    "canceled",
    "cancelled",
    "released",
    "release_complete",
}

_FORBIDDEN_KEYS = {
    "tenant_id",
    "team_id",
    "channel_id",
    "conversation_id",
    "activity_id",
    "user_id",
    "service_url",
    "web_url",
    "graph_url",
    "raw_activity_path",
    "raw_payload_path",
    "credential_ref",
    "secret_ref",
    "mount_ref",
    "provider_body",
    "command_output",
    "prompt",
}
_FORBIDDEN_VALUE_MARKERS = (
    "graph.microsoft.com",
    "smba.",
    "serviceurl",
    "service_url",
    "raw_activity",
    "raw_payload",
    "authorization",
    "bearer ",
    "secret",
    "token",
    "credential",
    "mount_ref",
)
_EXPLICIT_LINKED_NEW_WORK_RE = re.compile(
    r"\b(create|open|start|raise)\s+(a\s+)?(new\s+)?"
    r"(slice|story|work\s*item|follow[- ]?up|task)\b",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class ThreadRouteRecord:
    connector_type: str
    connector_id: str
    source_scope: str
    root_message_ref: str
    parent_work_item_id: str
    parent_work_item_type: str
    lifecycle_state: str | None
    owner_role: str | None
    source_anchor_ref: str | None = None
    terminal: bool = False
    created_at: str = ""
    schema_version: str = THREAD_ROUTE_SCHEMA_VERSION

    @staticmethod
    def create(
        *,
        connector_type: str,
        connector_id: str,
        source_scope: str,
        root_message_ref: str,
        parent_work_item_id: str,
        parent_work_item_type: str,
        lifecycle_state: str | None,
        owner_role: str | None,
        source_anchor_ref: str | None = None,
    ) -> "ThreadRouteRecord":
        return ThreadRouteRecord(
            connector_type=connector_type,
            connector_id=connector_id,
            source_scope=source_scope,
            root_message_ref=root_message_ref,
            parent_work_item_id=validate_logical_id(
                parent_work_item_id,
                field_name="parent_work_item_id",
            ),
            parent_work_item_type=parent_work_item_type or "slice",
            lifecycle_state=lifecycle_state,
            owner_role=owner_role,
            source_anchor_ref=source_anchor_ref,
            terminal=str(lifecycle_state or "").casefold() in TERMINAL_LIFECYCLE_STATES,
            created_at=utc_now_iso(),
        )

    @property
    def route_key(self) -> str:
        return route_key(
            connector_type=self.connector_type,
            connector_id=self.connector_id,
            source_scope=self.source_scope,
            root_message_ref=self.root_message_ref,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "connector_type": self.connector_type,
            "connector_id": self.connector_id,
            "source_scope": self.source_scope,
            "root_message_ref": _safe_ref(self.root_message_ref),
            "parent_work_item_id": self.parent_work_item_id,
            "parent_work_item_type": self.parent_work_item_type,
            "lifecycle_state": self.lifecycle_state,
            "owner_role": self.owner_role,
            "source_anchor_ref": self.source_anchor_ref,
            "terminal": self.terminal,
            "created_at": self.created_at,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "ThreadRouteRecord":
        return ThreadRouteRecord(
            connector_type=str(data["connector_type"]),
            connector_id=str(data["connector_id"]),
            source_scope=str(data["source_scope"]),
            root_message_ref=str(data.get("root_message_ref") or ""),
            parent_work_item_id=str(data["parent_work_item_id"]),
            parent_work_item_type=str(data.get("parent_work_item_type") or "slice"),
            lifecycle_state=(
                str(data["lifecycle_state"]) if data.get("lifecycle_state") else None
            ),
            owner_role=str(data["owner_role"]) if data.get("owner_role") else None,
            source_anchor_ref=(
                str(data["source_anchor_ref"]) if data.get("source_anchor_ref") else None
            ),
            terminal=bool(data.get("terminal", False)),
            created_at=str(data.get("created_at") or ""),
            schema_version=str(data.get("schema_version") or THREAD_ROUTE_SCHEMA_VERSION),
        )


@dataclass(frozen=True)
class ThreadedContext:
    context_id: str
    parent_work_item_id: str
    parent_work_item_type: str
    lifecycle_state: str | None
    owner_role: str | None
    connector_type: str
    connector_id: str
    source_scope: str
    source_anchor_ref: str | None
    idempotency_key_ref: str
    received_at: str
    actor_label: str
    sanitized_text: str
    summary: str
    action_state: str
    attention_state: str
    mentioned_roles: tuple[str, ...] = ()
    context_kind: str = "parent_context"
    related_work_item_id: str | None = None
    reason: str | None = None
    correlation_id: str | None = None
    schema_version: str = THREADED_CONTEXT_SCHEMA_VERSION

    def to_safe_dict(self) -> dict[str, Any]:
        data = {
            "schema_version": self.schema_version,
            "context_id": self.context_id,
            "parent_work_item_id": self.parent_work_item_id,
            "parent_work_item_type": self.parent_work_item_type,
            "lifecycle_state": self.lifecycle_state,
            "owner_role": self.owner_role,
            "connector_type": self.connector_type,
            "connector_id": self.connector_id,
            "source_scope": self.source_scope,
            "source_anchor_ref": self.source_anchor_ref,
            "idempotency_key_ref": self.idempotency_key_ref,
            "received_at": self.received_at,
            "actor_label": self.actor_label,
            "sanitized_text": self.sanitized_text,
            "summary": self.summary,
            "action_state": self.action_state,
            "attention_state": self.attention_state,
            "mentioned_roles": list(self.mentioned_roles),
            "context_kind": self.context_kind,
            "related_work_item_id": self.related_work_item_id,
            "reason": self.reason,
            "correlation_id": self.correlation_id,
        }
        _assert_safe_payload(data)
        return data

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "ThreadedContext":
        return ThreadedContext(
            context_id=str(data["context_id"]),
            parent_work_item_id=str(data["parent_work_item_id"]),
            parent_work_item_type=str(data.get("parent_work_item_type") or "slice"),
            lifecycle_state=(
                str(data["lifecycle_state"]) if data.get("lifecycle_state") else None
            ),
            owner_role=str(data["owner_role"]) if data.get("owner_role") else None,
            connector_type=str(data.get("connector_type") or "unknown"),
            connector_id=str(data.get("connector_id") or "unknown"),
            source_scope=str(data.get("source_scope") or "unknown"),
            source_anchor_ref=(
                str(data["source_anchor_ref"]) if data.get("source_anchor_ref") else None
            ),
            idempotency_key_ref=str(data["idempotency_key_ref"]),
            received_at=str(data.get("received_at") or ""),
            actor_label=str(data.get("actor_label") or "unknown"),
            sanitized_text=str(data.get("sanitized_text") or ""),
            summary=str(data.get("summary") or ""),
            action_state=str(data.get("action_state") or "captured"),
            attention_state=str(data.get("attention_state") or "not_required"),
            mentioned_roles=tuple(str(role) for role in data.get("mentioned_roles") or []),
            context_kind=str(data.get("context_kind") or "parent_context"),
            related_work_item_id=(
                str(data["related_work_item_id"])
                if data.get("related_work_item_id")
                else None
            ),
            reason=str(data["reason"]) if data.get("reason") else None,
            correlation_id=(
                str(data["correlation_id"]) if data.get("correlation_id") else None
            ),
            schema_version=str(
                data.get("schema_version") or THREADED_CONTEXT_SCHEMA_VERSION
            ),
        )


@dataclass(frozen=True)
class BindingResult:
    status: str
    context: ThreadedContext | None = None
    route: ThreadRouteRecord | None = None
    reason: str | None = None
    duplicate: bool = False
    conflict_count: int = 0


class FileThreadedContextStore:
    def __init__(self, state_root: Path, project_id: str, journal: EventJournal) -> None:
        self.root = state_root / "projects" / project_id / "threaded_context"
        self.project_id = project_id
        self.journal = journal
        for directory in [
            self.root / "contexts",
            self.root / "indexes" / "idempotency",
            self.root / "indexes" / "thread_routes",
            self.root / "support_access",
            self.root / "recovery",
            self.root / "locks",
        ]:
            directory.mkdir(parents=True, exist_ok=True)
            _chmod_private_dir(directory)

    def upsert_route(self, route: ThreadRouteRecord) -> None:
        path = self._route_path(route.route_key)
        if path.exists():
            return
        self._atomic_write(path, route.to_dict())
        self.journal.append(
            "threaded_context.route_indexed",
            project_id=self.project_id,
            connector_type=route.connector_type,
            connector_id=route.connector_id,
            source_scope=route.source_scope,
            parent_work_item_id=route.parent_work_item_id,
            parent_work_item_type=route.parent_work_item_type,
            lifecycle_state=route.lifecycle_state,
            owner_role=route.owner_role,
            source_anchor_ref=route.source_anchor_ref,
        )

    def resolve_route(
        self,
        *,
        connector_type: str,
        connector_id: str,
        source_scope: str,
        root_message_ref: str,
        candidate_connector_ids: list[str] | None = None,
    ) -> BindingResult:
        connector_ids = _unique_connector_ids(
            candidate_connector_ids or [connector_id],
            preferred=connector_id,
        )
        routes: list[ThreadRouteRecord] = []
        for candidate_connector_id in connector_ids:
            path = self._route_path(
                route_key(
                    connector_type=connector_type,
                    connector_id=candidate_connector_id,
                    source_scope=source_scope,
                    root_message_ref=root_message_ref,
                )
            )
            if not path.exists():
                continue
            try:
                routes.append(ThreadRouteRecord.from_dict(_read_json(path)))
            except Exception:
                self.journal.append(
                    "threaded_context.route_conflict",
                    project_id=self.project_id,
                    connector_type=connector_type,
                    connector_id=connector_id,
                    source_scope=source_scope,
                    reason="route_record_invalid",
                    candidate_count=1,
                    conflict_count=1,
                )
                return BindingResult(
                    status="conflict",
                    reason="route_record_invalid",
                    conflict_count=1,
                )
        if not routes:
            return BindingResult(status="not_verified", reason="parent_not_verified")
        parent_keys = {
            (
                route.parent_work_item_id,
                route.parent_work_item_type,
            )
            for route in routes
        }
        if len(parent_keys) > 1:
            self.journal.append(
                "threaded_context.route_conflict",
                project_id=self.project_id,
                connector_type=connector_type,
                connector_id=connector_id,
                source_scope=source_scope,
                reason="conflicting_parent_candidates",
                candidate_count=len(routes),
                conflict_count=len(parent_keys),
            )
            return BindingResult(
                status="conflict",
                reason="conflicting_parent_candidates",
                conflict_count=len(routes),
            )
        return BindingResult(status="bound", route=routes[0])

    def capture(
        self,
        *,
        route: ThreadRouteRecord,
        source_message_ref: str,
        actor_label: str | None,
        text: str,
        mentioned_roles: list[str],
        source_anchor_ref: str | None,
        correlation_id: str | None = None,
    ) -> BindingResult:
        sanitized = sanitize_text(text)
        idempotency_ref = idempotency_key_ref(
            parent_work_item_id=route.parent_work_item_id,
            actor_label=actor_label or "unknown",
            text=sanitized,
            source_message_ref=source_message_ref,
        )
        idempotency_path = self._idempotency_path(idempotency_ref)
        if idempotency_path.exists():
            data = _read_json(idempotency_path)
            context = self.get_context(
                str(data.get("parent_work_item_id") or route.parent_work_item_id),
                str(data.get("context_id") or ""),
            )
            self.journal.append(
                "threaded_context.duplicate_detected",
                project_id=self.project_id,
                connector_type=route.connector_type,
                connector_id=route.connector_id,
                parent_work_item_id=route.parent_work_item_id,
                idempotency_key_ref=idempotency_ref,
                result="duplicate",
                correlation_id=correlation_id,
            )
            return BindingResult(
                status="duplicate",
                context=context,
                route=route,
                reason="Already captured",
                duplicate=True,
            )

        explicit_link = has_explicit_linked_new_work_intent(sanitized)
        action_state = "needs_triage" if route.terminal else "captured"
        context = ThreadedContext(
            context_id=context_id_for(
                route.parent_work_item_id,
                idempotency_ref,
            ),
            parent_work_item_id=route.parent_work_item_id,
            parent_work_item_type=route.parent_work_item_type,
            lifecycle_state=route.lifecycle_state,
            owner_role=route.owner_role,
            connector_type=route.connector_type,
            connector_id=route.connector_id,
            source_scope=route.source_scope,
            source_anchor_ref=source_anchor_ref or route.source_anchor_ref,
            idempotency_key_ref=idempotency_ref,
            received_at=utc_now_iso(),
            actor_label=safe_actor_label(actor_label),
            sanitized_text=sanitized,
            summary=summarize_text(sanitized),
            action_state=action_state,
            attention_state="pending" if route.owner_role else "not_required",
            mentioned_roles=tuple(sorted(set(mentioned_roles))),
            context_kind="linked_new_work" if explicit_link else "parent_context",
            reason=(
                "explicit new-work wording was detected" if explicit_link else None
            ),
            correlation_id=correlation_id,
        )
        self._atomic_write(self._context_path(context), context.to_safe_dict())
        self._atomic_write(
            idempotency_path,
            {
                "schema_version": "threaded-context-idempotency-v0",
                "idempotency_key_ref": idempotency_ref,
                "context_id": context.context_id,
                "parent_work_item_id": context.parent_work_item_id,
                "created_at": context.received_at,
            },
        )
        self.journal.append(
            "threaded_context.captured",
            project_id=self.project_id,
            connector_type=context.connector_type,
            connector_id=context.connector_id,
            source_scope=context.source_scope,
            threaded_context_id=context.context_id,
            parent_work_item_id=context.parent_work_item_id,
            parent_work_item_type=context.parent_work_item_type,
            lifecycle_state=context.lifecycle_state,
            owner_role=context.owner_role,
            action_state=context.action_state,
            attention_state=context.attention_state,
            context_kind=context.context_kind,
            idempotency_key_ref=context.idempotency_key_ref,
            source_anchor_ref=context.source_anchor_ref,
            correlation_id=context.correlation_id,
        )
        return BindingResult(status="bound", context=context, route=route)

    def get_context(
        self,
        parent_work_item_id: str,
        context_id: str,
    ) -> ThreadedContext | None:
        if not context_id:
            return None
        path = (
            self.root
            / "contexts"
            / safe_filename(parent_work_item_id)
            / safe_filename(context_id)
        )
        if not path.exists():
            return None
        return ThreadedContext.from_dict(_read_json(path))

    def list_contexts(self, parent_work_item_id: str | None = None) -> list[ThreadedContext]:
        roots = []
        contexts_root = self.root / "contexts"
        if parent_work_item_id:
            roots.append(contexts_root / safe_filename(parent_work_item_id))
        elif contexts_root.exists():
            roots.extend(path for path in contexts_root.iterdir() if path.is_dir())
        rows = []
        for root in roots:
            for path in sorted(root.glob("*.json")):
                try:
                    rows.append(ThreadedContext.from_dict(_read_json(path)))
                except Exception:
                    continue
        return rows

    def summary_counts(self) -> dict[str, int]:
        counts = {
            "captured": 0,
            "pending_owner_attention": 0,
            "needs_triage": 0,
            "linked_new_work": 0,
        }
        for context in self.list_contexts():
            counts["captured"] += 1
            if context.attention_state == "pending":
                counts["pending_owner_attention"] += 1
            if context.action_state == "needs_triage":
                counts["needs_triage"] += 1
            if context.context_kind == "linked_new_work":
                counts["linked_new_work"] += 1
        return counts

    def support_access(
        self,
        *,
        actor: str | None,
        reason: str | None,
        authority: str | None,
        target_id: str | None,
        correlation_id: str | None,
    ) -> dict[str, Any]:
        missing = [
            name
            for name, value in {
                "actor": actor,
                "reason": reason,
                "authority": authority,
                "target_id": target_id,
                "correlation_id": correlation_id,
            }.items()
            if not value
        ]
        if missing:
            self.journal.append(
                "threaded_context.support_access",
                project_id=self.project_id,
                result="denied",
                missing_fields=missing,
            )
            raise PermissionError("support-mode access requires explicit audit fields")
        record = {
            "schema_version": SUPPORT_ACCESS_SCHEMA_VERSION,
            "support_access_id": _digest("|".join([actor or "", target_id or "", correlation_id or ""])),
            "actor": safe_actor_label(actor),
            "reason": summarize_text(reason or ""),
            "authority": summarize_text(authority or ""),
            "target_id": validate_logical_id(str(target_id), field_name="target_id"),
            "correlation_id": correlation_id,
            "accessed_at": utc_now_iso(),
        }
        self._atomic_write(
            self.root / "support_access" / safe_filename(record["support_access_id"]),
            record,
        )
        self.journal.append(
            "threaded_context.support_access",
            project_id=self.project_id,
            result="authorized",
            target_id=record["target_id"],
            support_access_id=record["support_access_id"],
            correlation_id=correlation_id,
        )
        return record

    def purge_expired_raw(
        self,
        *,
        actor: str,
        reason: str,
        older_than: str,
        correlation_id: str,
        dry_run: bool,
    ) -> dict[str, Any]:
        result = {
            "schema_version": "threaded-context-purge-v0",
            "dry_run": dry_run,
            "purged": 0,
            "retained_contexts": len(self.list_contexts()),
            "older_than": older_than,
        }
        self.journal.append(
            "threaded_context.purge",
            project_id=self.project_id,
            actor=safe_actor_label(actor),
            reason=summarize_text(reason),
            dry_run=dry_run,
            purged_count=0,
            retained_context_count=result["retained_contexts"],
            correlation_id=correlation_id,
        )
        return result

    def rebuild_indexes(self, *, dry_run: bool, correlation_id: str) -> dict[str, Any]:
        contexts = self.list_contexts()
        rebuilt = 0
        if not dry_run:
            for context in contexts:
                self._atomic_write(
                    self._idempotency_path(context.idempotency_key_ref),
                    {
                        "schema_version": "threaded-context-idempotency-v0",
                        "idempotency_key_ref": context.idempotency_key_ref,
                        "context_id": context.context_id,
                        "parent_work_item_id": context.parent_work_item_id,
                        "created_at": context.received_at,
                    },
                )
                rebuilt += 1
        self.journal.append(
            "threaded_context.index_rebuild",
            project_id=self.project_id,
            dry_run=dry_run,
            rebuilt_count=rebuilt if not dry_run else len(contexts),
            conflict_count=0,
            correlation_id=correlation_id,
        )
        return {
            "schema_version": "threaded-context-index-rebuild-v0",
            "dry_run": dry_run,
            "candidate_count": len(contexts),
            "rebuilt_count": rebuilt if not dry_run else len(contexts),
            "conflict_count": 0,
        }

    def recover_known_relationship(
        self,
        *,
        parent_work_item_id: str,
        stray_work_item_id: str,
        verified: bool,
        actor: str,
        reason: str,
        correlation_id: str,
    ) -> dict[str, Any]:
        parent_id = validate_logical_id(parent_work_item_id, field_name="parent_work_item_id")
        stray_id = validate_logical_id(stray_work_item_id, field_name="stray_work_item_id")
        status = "linked" if verified else "recovery_blocked"
        record = {
            "schema_version": RECOVERY_SCHEMA_VERSION,
            "recovery_id": f"recovery-{_digest(parent_id + stray_id)[:24]}",
            "parent_work_item_id": parent_id,
            "stray_work_item_id": stray_id,
            "status": status,
            "actor": safe_actor_label(actor),
            "reason": summarize_text(reason),
            "correlation_id": correlation_id,
            "created_at": utc_now_iso(),
        }
        self._atomic_write(self.root / "recovery" / safe_filename(record["recovery_id"]), record)
        self.journal.append(
            "threaded_context.recovery.linked"
            if verified
            else "threaded_context.recovery.blocked",
            project_id=self.project_id,
            parent_work_item_id=parent_id,
            stray_work_item_id=stray_id,
            recovery_id=record["recovery_id"],
            status=status,
            correlation_id=correlation_id,
        )
        return record

    def _route_path(self, key: str) -> Path:
        return self.root / "indexes" / "thread_routes" / safe_filename(key)

    def _idempotency_path(self, key_ref: str) -> Path:
        return self.root / "indexes" / "idempotency" / safe_filename(key_ref)

    def _context_path(self, context: ThreadedContext) -> Path:
        return (
            self.root
            / "contexts"
            / safe_filename(context.parent_work_item_id)
            / safe_filename(context.context_id)
        )

    @staticmethod
    def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
        _assert_contained(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        _chmod_private_dir(path.parent)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.chmod(temp_name, 0o600)
            Path(temp_name).replace(path)
        finally:
            temp_path = Path(temp_name)
            if temp_path.exists():
                temp_path.unlink()


def route_key(
    *,
    connector_type: str,
    connector_id: str,
    source_scope: str,
    root_message_ref: str,
) -> str:
    digest = _digest(
        "|".join(
            [
                connector_type,
                connector_id,
                source_scope,
                str(root_message_ref),
            ]
        )
    )
    return f"route-{digest[:32]}"


def context_id_for(parent_work_item_id: str, idempotency_ref: str) -> str:
    return f"tctx-{_digest(parent_work_item_id + ':' + idempotency_ref)[:24]}"


def idempotency_key_ref(
    *,
    parent_work_item_id: str,
    actor_label: str,
    text: str,
    source_message_ref: str,
) -> str:
    # Connector deliveries can use different ids for the same human reply.
    # Keep this key anchored to the verified parent plus safe reply content.
    _ = source_message_ref
    stable = "|".join(
        [
            parent_work_item_id,
            safe_actor_label(actor_label).casefold(),
            re.sub(r"\s+", " ", text).casefold(),
        ]
    )
    return f"idem-{_digest(stable)[:32]}"


def _unique_connector_ids(values: list[str], *, preferred: str) -> list[str]:
    ordered = [preferred, *values]
    seen: set[str] = set()
    unique: list[str] = []
    for value in ordered:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        unique.append(text)
    return unique


def sanitize_text(value: Any, *, max_length: int = 2000) -> str:
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    for marker in _FORBIDDEN_VALUE_MARKERS:
        text = re.sub(re.escape(marker), "[redacted]", text, flags=re.IGNORECASE)
    if len(text) > max_length:
        text = text[: max_length - 1].rstrip() + "..."
    return text or "redacted_unavailable"


def summarize_text(value: Any, *, max_length: int = 240) -> str:
    text = sanitize_text(value, max_length=max_length)
    if len(text) <= max_length:
        return text
    return text[: max_length - 1].rstrip() + "..."


def safe_actor_label(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "teams-user")).strip()
    if not text:
        return "teams-user"
    return summarize_text(text, max_length=80)


def has_explicit_linked_new_work_intent(text: str) -> bool:
    return bool(_EXPLICIT_LINKED_NEW_WORK_RE.search(text))


def _assert_safe_payload(payload: dict[str, Any]) -> None:
    rendered = json.dumps(payload, sort_keys=True).casefold()
    for key in _FORBIDDEN_KEYS:
        if f'"{key}"' in rendered:
            raise ValueError(f"threaded context default payload includes forbidden key {key}")
    for marker in _FORBIDDEN_VALUE_MARKERS:
        if marker in rendered:
            raise ValueError("threaded context default payload includes forbidden value")
    for value in payload.values():
        if isinstance(value, str) and Path(value).is_absolute():
            raise ValueError("threaded context default payload includes absolute path")


def _assert_contained(path: Path) -> None:
    if path.is_absolute() and "threaded_context" not in path.parts:
        raise ValueError("threaded context path is outside contained state")
    if ".." in path.parts:
        raise ValueError("threaded context path must be contained")


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("expected object")
    return data


def _safe_ref(value: str) -> str:
    return f"ref:{_digest(str(value))[:24]}"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _chmod_private_dir(path: Path) -> None:
    try:
        path.chmod(0o700)
    except OSError:
        pass
