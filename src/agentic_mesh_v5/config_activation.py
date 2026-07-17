from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import threading
from typing import Callable, Iterator, Mapping, Sequence
from uuid import uuid4

from agentic_mesh_v5.package_resolver import PackageResolutionError
from agentic_mesh_v5.package_resolver import ResolvedConfiguration
from agentic_mesh_v5.package_resolver import calculate_configuration_digest
from agentic_mesh_v5.package_resolver import resolve_packages


DIGEST = re.compile(r"^[0-9a-f]{64}$")
_UNSET = object()


class ConfigActivationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ConfigRelease:
    digest: str
    created_at: str
    created_by: str
    packages: tuple[str, ...]
    resolved: Mapping[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "digest": self.digest,
            "created_at": self.created_at,
            "created_by": self.created_by,
            "packages": list(self.packages),
            "resolved": self.resolved,
        }


@dataclass(frozen=True, slots=True)
class ActivationState:
    revision: int
    active_digest: str | None
    history: tuple[Mapping[str, object], ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "revision": self.revision,
            "active_digest": self.active_digest,
            "history": list(self.history),
        }


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class ConfigActivationStore:
    def __init__(
        self,
        repository_root: Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository_root = repository_root.resolve()
        if not self.repository_root.is_dir():
            raise ConfigActivationError(
                f"configuration repository is not a directory: {self.repository_root}"
            )
        self.releases_dir = self.repository_root / "releases"
        self.activation_path = self.repository_root / "activation" / "current.json"
        self.lock_path = self.repository_root / "state" / "config-activation.lock"
        self._clock = clock or (lambda: datetime.now(UTC))
        self._thread_lock = threading.Lock()

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self._thread_lock:
            with _file_lock(self.lock_path):
                yield

    def _timestamp(self) -> str:
        timestamp = self._clock()
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        return timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _actor(value: str) -> str:
        actor = value.strip()
        if not actor:
            raise ConfigActivationError("actor must not be empty")
        return actor

    @staticmethod
    def _digest(value: str) -> str:
        if DIGEST.fullmatch(value) is None:
            raise ConfigActivationError(f"invalid release digest: {value}")
        return value

    def validate_draft(self, references: Sequence[str]) -> ResolvedConfiguration:
        try:
            return resolve_packages(self.repository_root, references)
        except PackageResolutionError as exc:
            raise ConfigActivationError(str(exc)) from exc

    def create_release(
        self,
        references: Sequence[str],
        *,
        actor: str,
    ) -> ConfigRelease:
        actor = self._actor(actor)
        resolved = self.validate_draft(references)
        release = ConfigRelease(
            digest=resolved.digest,
            created_at=self._timestamp(),
            created_by=actor,
            packages=resolved.packages,
            resolved=resolved.to_dict(),
        )
        with self._locked():
            path = self._release_path(release.digest)
            if path.exists():
                existing = self._read_release(path)
                if existing.resolved != release.resolved:
                    raise ConfigActivationError(
                        f"immutable release conflicts with digest {release.digest}"
                    )
                return existing
            self.releases_dir.mkdir(parents=True, exist_ok=True)
            self._atomic_write(path, release.to_dict())
        return release

    def get_release(self, digest: str) -> ConfigRelease:
        digest = self._digest(digest)
        with self._locked():
            return self._read_release(self._release_path(digest))

    def get_state(self) -> ActivationState:
        with self._locked():
            state = self._read_state()
            if state.active_digest is not None:
                self._read_release(self._release_path(state.active_digest))
            return state

    def activate(
        self,
        digest: str,
        *,
        actor: str,
        reason: str = "",
        expected_active: str | None | object = _UNSET,
    ) -> ActivationState:
        return self._change_active(
            digest,
            actor=actor,
            reason=reason,
            expected_active=expected_active,
            action="activate",
        )

    def rollback(
        self,
        digest: str,
        *,
        actor: str,
        reason: str,
    ) -> ActivationState:
        if not reason.strip():
            raise ConfigActivationError("rollback reason must not be empty")
        return self._change_active(
            digest,
            actor=actor,
            reason=reason,
            expected_active=_UNSET,
            action="rollback",
        )

    def _change_active(
        self,
        digest: str,
        *,
        actor: str,
        reason: str,
        expected_active: str | None | object,
        action: str,
    ) -> ActivationState:
        digest = self._digest(digest)
        actor = self._actor(actor)
        if expected_active is not _UNSET and expected_active is not None:
            if not isinstance(expected_active, str):
                raise ConfigActivationError("expected active digest must be a string or null")
            expected_active = self._digest(expected_active)
        with self._locked():
            self._read_release(self._release_path(digest))
            state = self._read_state()
            if expected_active is not _UNSET and state.active_digest != expected_active:
                raise ConfigActivationError(
                    "active release changed: "
                    f"expected {expected_active}, found {state.active_digest}"
                )
            if state.active_digest == digest:
                return state
            if action == "rollback" and state.active_digest is None:
                raise ConfigActivationError("cannot roll back without an active release")
            revision = state.revision + 1
            event: dict[str, object] = {
                "revision": revision,
                "action": action,
                "actor": actor,
                "reason": reason.strip(),
                "previous_digest": state.active_digest,
                "target_digest": digest,
                "timestamp": self._timestamp(),
            }
            updated = ActivationState(
                revision=revision,
                active_digest=digest,
                history=(*state.history, event),
            )
            self.activation_path.parent.mkdir(parents=True, exist_ok=True)
            self._atomic_write(self.activation_path, updated.to_dict())
            return updated

    def _release_path(self, digest: str) -> Path:
        return self.releases_dir / f"{self._digest(digest)}.json"

    def _read_release(self, path: Path) -> ConfigRelease:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ConfigActivationError(f"release not found: {path.stem}") from exc
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ConfigActivationError(f"invalid release record {path.name}: {exc}") from exc
        required = {
            "schema_version",
            "digest",
            "created_at",
            "created_by",
            "packages",
            "resolved",
        }
        if not isinstance(payload, dict) or set(payload) != required:
            raise ConfigActivationError(f"invalid release record: {path.name}")
        digest = self._digest(str(payload["digest"]))
        resolved = payload["resolved"]
        packages = payload["packages"]
        if (
            payload["schema_version"] != 1
            or digest != path.stem
            or not isinstance(resolved, dict)
            or resolved.get("digest") != digest
            or calculate_configuration_digest(resolved) != digest
            or not isinstance(packages, list)
            or not all(isinstance(item, str) for item in packages)
            or packages != resolved.get("packages")
            or not isinstance(payload["created_at"], str)
            or not isinstance(payload["created_by"], str)
        ):
            raise ConfigActivationError(f"invalid release record: {path.name}")
        return ConfigRelease(
            digest=digest,
            created_at=payload["created_at"],
            created_by=payload["created_by"],
            packages=tuple(packages),
            resolved=resolved,
        )

    def _read_state(self) -> ActivationState:
        if not self.activation_path.exists():
            return ActivationState(revision=0, active_digest=None, history=())
        try:
            payload = json.loads(self.activation_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ConfigActivationError(f"invalid activation state: {exc}") from exc
        if not isinstance(payload, dict) or set(payload) != {
            "schema_version",
            "revision",
            "active_digest",
            "history",
        }:
            raise ConfigActivationError("invalid activation state")
        revision = payload["revision"]
        active_digest = payload["active_digest"]
        history = payload["history"]
        if (
            payload["schema_version"] != 1
            or not isinstance(revision, int)
            or revision < 0
            or (
                active_digest is not None
                and (
                    not isinstance(active_digest, str)
                    or DIGEST.fullmatch(active_digest) is None
                )
            )
            or not isinstance(history, list)
            or len(history) != revision
            or not all(isinstance(event, dict) for event in history)
        ):
            raise ConfigActivationError("invalid activation state")
        previous: str | None = None
        event_fields = {
            "revision",
            "action",
            "actor",
            "reason",
            "previous_digest",
            "target_digest",
            "timestamp",
        }
        for expected_revision, event in enumerate(history, start=1):
            target = event.get("target_digest")
            if (
                set(event) != event_fields
                or event.get("revision") != expected_revision
                or event.get("action") not in {"activate", "rollback"}
                or not isinstance(event.get("actor"), str)
                or not event["actor"].strip()
                or not isinstance(event.get("reason"), str)
                or event.get("previous_digest") != previous
                or not isinstance(target, str)
                or DIGEST.fullmatch(target) is None
                or not isinstance(event.get("timestamp"), str)
                or not event["timestamp"].strip()
                or (
                    event.get("action") == "rollback"
                    and (previous is None or not event["reason"].strip())
                )
            ):
                raise ConfigActivationError("invalid activation state history")
            previous = target
        if (revision == 0 and active_digest is not None) or (
            revision > 0 and previous != active_digest
        ):
            raise ConfigActivationError("activation state is not supported by its history")
        return ActivationState(revision, active_digest, tuple(history))

    @staticmethod
    def _atomic_write(path: Path, payload: Mapping[str, object]) -> None:
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            try:
                directory = os.open(path.parent, os.O_RDONLY)
            except OSError:
                directory = None
            if directory is not None:
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        except OSError as exc:
            raise ConfigActivationError(f"atomic write failed for {path.name}: {exc}") from exc
        finally:
            if temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    pass
