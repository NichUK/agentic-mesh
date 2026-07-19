from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, Literal, Mapping, Sequence

from agentic_mesh_v5.config_activation import ConfigActivationError
from agentic_mesh_v5.config_activation import ConfigActivationStore


IDENTIFIER = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")


class ConfigPromotionError(ValueError):
    pass


class ConfigPromotionNotFound(ConfigPromotionError):
    pass


class ConfigPromotionConflict(ConfigPromotionError):
    pass


@dataclass(frozen=True, slots=True)
class ConfigDraft:
    project_id: str
    draft_id: str
    references: tuple[str, ...]
    expected_active_digest: str | None
    created_by: str
    created_at: str
    status: str
    validation: Mapping[str, object] | None
    decision: Mapping[str, object] | None
    events: tuple[Mapping[str, object], ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "project_id": self.project_id,
            "draft_id": self.draft_id,
            "references": list(self.references),
            "expected_active_digest": self.expected_active_digest,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "status": self.status,
            "validation": self.validation,
            "decision": self.decision,
            "events": list(self.events),
        }


class ConfigPromotionStore:
    def __init__(
        self,
        activation: ConfigActivationStore,
        *,
        project_id: str,
    ) -> None:
        if not isinstance(activation, ConfigActivationStore):
            raise ConfigPromotionError("configuration activation store is invalid")
        self.activation = activation
        self.project_id = _identifier(project_id, "project_id")
        self.drafts_dir = (
            activation.repository_root / "promotions" / "projects" / self.project_id
        )

    def create(
        self,
        *,
        draft_id: str,
        references: Sequence[str],
        expected_active_digest: str | None,
        actor: str,
    ) -> ConfigDraft:
        draft_id = _identifier(draft_id, "draft_id")
        actor = _required(actor, "actor")
        references = _references(references)
        expected_active_digest = _optional_digest(expected_active_digest)
        draft = ConfigDraft(
            self.project_id,
            draft_id,
            references,
            expected_active_digest,
            actor,
            self.activation._timestamp(),
            "draft",
            None,
            None,
            (),
        )
        with self.activation._locked():
            path = self._path(draft_id)
            if path.exists():
                existing = self._read(path)
                if (
                    existing.references != draft.references
                    or existing.expected_active_digest != expected_active_digest
                    or existing.created_by != actor
                ):
                    raise ConfigPromotionConflict("configuration draft id conflicts")
                return existing
            if self.activation._read_state().active_digest != expected_active_digest:
                raise ConfigPromotionConflict(
                    "active configuration differs from draft baseline"
                )
            self.drafts_dir.mkdir(parents=True, exist_ok=True)
            self.activation._atomic_write(path, draft.to_dict())
        return draft

    def get(self, draft_id: str) -> ConfigDraft:
        with self.activation._locked():
            return self._read(self._path(_identifier(draft_id, "draft_id")))

    def validate(
        self, draft_id: str, *, actor: str, sponsor_authored: bool
    ) -> ConfigDraft:
        actor = _required(actor, "actor")
        with self.activation._locked():
            path = self._path(_identifier(draft_id, "draft_id"))
            draft = self._read(path)
            state = self.activation._read_state()
            if state.active_digest != draft.expected_active_digest:
                raise ConfigPromotionConflict("active configuration changed since draft")
            try:
                resolved = self.activation.validate_draft(draft.references)
            except ConfigActivationError as exc:
                raise ConfigPromotionConflict(str(exc)) from exc
            active = (
                {}
                if state.active_digest is None
                else self.activation._read_release(
                    self.activation._release_path(state.active_digest)
                ).resolved
            )
            if draft.status != "draft":
                existing = draft.validation or {}
                if (
                    existing.get("digest") == resolved.digest
                    and existing.get("active_digest") == state.active_digest
                ):
                    return draft
                raise ConfigPromotionConflict("configuration draft is already validated")
            validation = {
                "digest": resolved.digest,
                "resolved": resolved.to_dict(),
                "active_digest": state.active_digest,
                "diff": structural_diff(active, resolved.to_dict()),
                "validated_by": actor,
                "validated_at": self.activation._timestamp(),
            }
            target_status = "approved" if sponsor_authored else "pending_approval"
            decision = (
                {
                    "decision": "approved",
                    "decided_by": draft.created_by,
                    "rationale": "Sponsor-authored validated draft",
                    "decided_at": validation["validated_at"],
                    "implicit": True,
                }
                if sponsor_authored
                else None
            )
            updated = ConfigDraft(
                draft.project_id,
                draft.draft_id,
                draft.references,
                draft.expected_active_digest,
                draft.created_by,
                draft.created_at,
                target_status,
                validation,
                decision,
                (
                    *draft.events,
                    _event("validated", actor, validation["validated_at"]),
                    *(
                        (_event("approved", draft.created_by, validation["validated_at"]),)
                        if sponsor_authored
                        else ()
                    ),
                ),
            )
            self.activation._atomic_write(path, updated.to_dict())
            return updated

    def decide(
        self,
        draft_id: str,
        *,
        sponsor_id: str,
        decision: Literal["approved", "rejected"],
        rationale: str,
    ) -> ConfigDraft:
        sponsor_id = _required(sponsor_id, "sponsor_id")
        rationale = _required(rationale, "rationale")
        if decision not in {"approved", "rejected"}:
            raise ValueError("decision must be approved or rejected")
        with self.activation._locked():
            path = self._path(_identifier(draft_id, "draft_id"))
            draft = self._read(path)
            record = {
                "decision": decision,
                "decided_by": sponsor_id,
                "rationale": rationale,
                "decided_at": self.activation._timestamp(),
                "implicit": False,
            }
            target_status = decision
            if draft.status != "pending_approval":
                existing = draft.decision or {}
                if (
                    draft.status == target_status
                    and existing.get("decision") == decision
                    and existing.get("decided_by") == sponsor_id
                    and existing.get("rationale") == rationale
                    and existing.get("implicit") is False
                ):
                    return draft
                raise ConfigPromotionConflict("configuration draft is not awaiting approval")
            updated = ConfigDraft(
                draft.project_id,
                draft.draft_id,
                draft.references,
                draft.expected_active_digest,
                draft.created_by,
                draft.created_at,
                target_status,
                draft.validation,
                record,
                (*draft.events, _event(decision, sponsor_id, record["decided_at"])),
            )
            self.activation._atomic_write(path, updated.to_dict())
            return updated

    def activate(self, draft_id: str, *, actor: str, reason: str) -> ConfigDraft:
        actor = _required(actor, "actor")
        draft = self.get(draft_id)
        if draft.status == "activated":
            return draft
        if draft.status != "approved" or draft.validation is None:
            raise ConfigPromotionConflict("configuration draft is not approved")
        current = self.activation.validate_draft(draft.references)
        if current.digest != draft.validation["digest"]:
            raise ConfigPromotionConflict("validated package content changed")
        release = self.activation.create_release(draft.references, actor=actor)
        if self.activation.get_state().active_digest != release.digest:
            self.activation.activate(
                release.digest,
                actor=actor,
                reason=reason,
                expected_active=draft.expected_active_digest,
            )
        with self.activation._locked():
            path = self._path(draft.draft_id)
            latest = self._read(path)
            if latest.status == "activated":
                return latest
            if latest.status != "approved" or latest.validation != draft.validation:
                raise ConfigPromotionConflict("configuration draft changed during activation")
            timestamp = self.activation._timestamp()
            updated = ConfigDraft(
                latest.project_id,
                latest.draft_id,
                latest.references,
                latest.expected_active_digest,
                latest.created_by,
                latest.created_at,
                "activated",
                latest.validation,
                latest.decision,
                (*latest.events, _event("activated", actor, timestamp)),
            )
            self.activation._atomic_write(path, updated.to_dict())
            return updated

    def rollback(self, digest: str, *, sponsor_id: str, reason: str):
        return self.activation.rollback(
            _digest(digest), actor=_required(sponsor_id, "sponsor_id"), reason=reason
        )

    def _path(self, draft_id: str) -> Path:
        return self.drafts_dir / f"{draft_id}.json"

    def _read(self, path: Path) -> ConfigDraft:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ConfigPromotionNotFound("configuration draft not found") from exc
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ConfigPromotionError("configuration draft record is invalid") from exc
        required = {
            "schema_version", "project_id", "draft_id", "references",
            "expected_active_digest", "created_by", "created_at", "status",
            "validation", "decision", "events",
        }
        if not isinstance(value, dict) or set(value) != required:
            raise ConfigPromotionError("configuration draft record is invalid")
        draft = ConfigDraft(
            _identifier(value["project_id"], "project_id"),
            _identifier(value["draft_id"], "draft_id"),
            _references(value["references"]),
            _optional_digest(value["expected_active_digest"]),
            _required(value["created_by"], "created_by"),
            _required(value["created_at"], "created_at"),
            _required(value["status"], "status"),
            value["validation"],
            value["decision"],
            tuple(value["events"]),
        )
        if (
            value["schema_version"] != 1
            or draft.project_id != self.project_id
            or draft.status not in {
                "draft", "pending_approval", "approved", "rejected", "activated"
            }
            or not isinstance(draft.events, tuple)
            or not all(isinstance(item, dict) for item in draft.events)
            or (draft.status == "draft") != (draft.validation is None)
            or (draft.status in {"approved", "rejected", "activated"})
                != (draft.decision is not None)
            or (draft.validation is not None and not isinstance(draft.validation, dict))
            or (draft.decision is not None and not isinstance(draft.decision, dict))
            or not _valid_validation(draft)
            or not _valid_decision(draft)
            or not _valid_events(draft.events)
        ):
            raise ConfigPromotionError("configuration draft record is invalid")
        return draft


def structural_diff(before: Mapping[str, object], after: Mapping[str, object]):
    left = _flatten(before)
    right = _flatten(after)
    result = []
    missing = object()
    for path in sorted(set(left) | set(right)):
        old = left.get(path, missing)
        new = right.get(path, missing)
        if old == new:
            continue
        result.append(
            {
                "path": path,
                "change": (
                    "added" if old is missing else "removed" if new is missing else "changed"
                ),
                "before": None if old is missing else old,
                "after": None if new is missing else new,
            }
        )
    return result


def _flatten(value: object, path: str = "$") -> dict[str, object]:
    if isinstance(value, Mapping):
        result = {}
        for key in sorted(value):
            result.update(_flatten(value[key], f"{path}.{key}"))
        return result
    if isinstance(value, list):
        result = {}
        for index, item in enumerate(value):
            result.update(_flatten(item, f"{path}[{index}]"))
        return result
    return {path: value}


def _event(action: str, actor: str, timestamp: object) -> dict[str, object]:
    return {"action": action, "actor": actor, "timestamp": timestamp}


def _valid_validation(draft: ConfigDraft) -> bool:
    value = draft.validation
    if value is None:
        return draft.status == "draft"
    if set(value) != {
        "digest", "resolved", "active_digest", "diff", "validated_by", "validated_at"
    }:
        return False
    resolved = value["resolved"]
    return bool(
        isinstance(value["digest"], str)
        and DIGEST.fullmatch(value["digest"])
        and isinstance(resolved, dict)
        and resolved.get("digest") == value["digest"]
        and value["active_digest"] == draft.expected_active_digest
        and isinstance(value["diff"], list)
        and all(
            isinstance(item, dict)
            and set(item) == {"path", "change", "before", "after"}
            and item["change"] in {"added", "removed", "changed"}
            for item in value["diff"]
        )
        and isinstance(value["validated_by"], str)
        and bool(value["validated_by"].strip())
        and isinstance(value["validated_at"], str)
        and bool(value["validated_at"].strip())
    )


def _valid_decision(draft: ConfigDraft) -> bool:
    value = draft.decision
    if value is None:
        return draft.status in {"draft", "pending_approval"}
    if set(value) != {
        "decision", "decided_by", "rationale", "decided_at", "implicit"
    }:
        return False
    expected = "rejected" if draft.status == "rejected" else "approved"
    return bool(
        draft.status in {"approved", "rejected", "activated"}
        and value["decision"] == expected
        and isinstance(value["decided_by"], str)
        and bool(value["decided_by"].strip())
        and isinstance(value["rationale"], str)
        and bool(value["rationale"].strip())
        and isinstance(value["decided_at"], str)
        and bool(value["decided_at"].strip())
        and isinstance(value["implicit"], bool)
    )


def _valid_events(events: tuple[Mapping[str, object], ...]) -> bool:
    return all(
        set(item) == {"action", "actor", "timestamp"}
        and item["action"] in {"validated", "approved", "rejected", "activated"}
        and isinstance(item["actor"], str)
        and bool(item["actor"].strip())
        and isinstance(item["timestamp"], str)
        and bool(item["timestamp"].strip())
        for item in events
    )


def _identifier(value: object, field: str) -> str:
    value = _required(value, field)
    if IDENTIFIER.fullmatch(value) is None:
        raise ConfigPromotionError(f"{field} is invalid")
    return value


def _required(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigPromotionError(f"{field} is required")
    return value.strip()


def _references(value: Sequence[str]) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        raise ConfigPromotionError("references must be an array")
    items = tuple(_required(item, "reference") for item in value)
    if not items or len(items) != len(set(items)):
        raise ConfigPromotionError("references must be non-empty and unique")
    return items


def _optional_digest(value: object) -> str | None:
    return None if value is None else _digest(value)


def _digest(value: object) -> str:
    value = _required(value, "digest")
    if DIGEST.fullmatch(value) is None:
        raise ConfigPromotionError("digest is invalid")
    return value
