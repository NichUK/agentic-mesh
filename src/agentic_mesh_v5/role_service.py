from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
from threading import Event, Lock, Thread
from time import monotonic
from typing import Callable, Literal, Mapping

import psycopg

from agentic_mesh_v5.codex_provider import CodexProviderConfig, CodexWorkerProvider
from agentic_mesh_v5.database import DatabaseConfigurationError, DatabaseError, SCHEMA
from agentic_mesh_v5.progress import reject_sensitive_content
from agentic_mesh_v5.project_registration import ProjectRegistrationCoordinator
from agentic_mesh_v5.prompt_renderer import RenderedRoleStatePrompt, render_role_state_prompt
from agentic_mesh_v5.queues import LeaseClaim, RoleQueueStore
from agentic_mesh_v5.thread_affinity import (
    ThreadAffinityCoordinator,
    ThreadAffinityKey,
    ThreadAffinityStore,
)
from agentic_mesh_v5.warm_engines import RoleInstanceKey, WarmEnginePool
from agentic_mesh_v5.worker_provider import (
    ProviderEventKind,
    SandboxPolicy,
    ThreadRequest,
    TurnCompletionStatus,
    TurnRequest,
    WorkerProvider,
    WorkerThread,
    WorkerTurn,
)


_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_CONVERSATION = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_SAFE_FAILURE = "role service turn failed"
_CODEX_SANDBOX_ENV = "AGENTIC_MESH_V5_CODEX_SANDBOX"


def _codex_sandbox_policy() -> SandboxPolicy:
    """Return the Codex sandbox policy from the environment.

    Defaults to ``full_access`` because the worker container is the
    project/tool isolation boundary and nested bubblewrap cannot create
    namespaces inside it.  Set ``AGENTIC_MESH_V5_CODEX_SANDBOX`` to
    ``read_only`` or ``workspace_write`` to use a stricter policy in
    deployments where additional sandboxing is available.
    """
    value = os.environ.get(_CODEX_SANDBOX_ENV, "").strip()
    if value:
        try:
            return SandboxPolicy(value)
        except ValueError:
            valid = ", ".join(p.value for p in SandboxPolicy)
            raise RoleServiceConfigurationError(
                f"{_CODEX_SANDBOX_ENV} must be one of: {valid}"
            ) from None
    return SandboxPolicy.FULL_ACCESS


class RoleServiceError(RuntimeError):
    pass


class RoleServiceConfigurationError(RoleServiceError):
    pass


@dataclass(frozen=True, slots=True)
class RoleServiceConfig:
    project_id: str
    role_id: str
    instance_id: str
    configuration_root: Path
    source_repositories_file: Path
    lease_seconds: int = 120
    heartbeat_seconds: int = 30
    turn_timeout_seconds: int = 900
    poll_seconds: float = 2.0

    def __post_init__(self) -> None:
        for field in ("project_id", "role_id", "instance_id"):
            value = getattr(self, field)
            if not isinstance(value, str) or _ID.fullmatch(value) is None:
                raise RoleServiceConfigurationError(f"{field} is invalid")
        for field in ("configuration_root", "source_repositories_file"):
            value = getattr(self, field)
            if not isinstance(value, Path) or not value.is_absolute():
                raise RoleServiceConfigurationError(f"{field} must be absolute")
        if not self.configuration_root.is_dir():
            raise RoleServiceConfigurationError("configuration_root is unavailable")
        if not self.source_repositories_file.is_file():
            raise RoleServiceConfigurationError("source repository bindings are unavailable")
        if (
            isinstance(self.lease_seconds, bool)
            or not isinstance(self.lease_seconds, int)
            or self.lease_seconds < 30
            or self.lease_seconds > 3600
        ):
            raise RoleServiceConfigurationError("lease_seconds is invalid")
        if (
            isinstance(self.heartbeat_seconds, bool)
            or not isinstance(self.heartbeat_seconds, int)
            or self.heartbeat_seconds < 5
            or self.heartbeat_seconds * 2 >= self.lease_seconds
        ):
            raise RoleServiceConfigurationError("heartbeat_seconds is invalid")
        if (
            isinstance(self.turn_timeout_seconds, bool)
            or not isinstance(self.turn_timeout_seconds, int)
            or self.turn_timeout_seconds < 1
            or self.turn_timeout_seconds > 14_400
        ):
            raise RoleServiceConfigurationError("turn_timeout_seconds is invalid")
        if (
            isinstance(self.poll_seconds, bool)
            or not isinstance(self.poll_seconds, (int, float))
            or not math.isfinite(float(self.poll_seconds))
            or self.poll_seconds <= 0
            or self.poll_seconds > 60
        ):
            raise RoleServiceConfigurationError("poll_seconds is invalid")


@dataclass(frozen=True, slots=True)
class RoleServiceResult:
    status: Literal["empty", "completed", "released"]
    project_id: str
    role_id: str
    instance_id: str
    queue_item_id: str | None = None
    work_item_id: str | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "project_id": self.project_id,
            "role_id": self.role_id,
            "instance_id": self.instance_id,
            "queue_item_id": self.queue_item_id,
            "work_item_id": self.work_item_id,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class _WorkContext:
    role_reference: str
    flow_reference: str
    prompt_digest: str
    state_id: str


class _LeaseHeartbeat:
    def __init__(
        self,
        queues: RoleQueueStore,
        claim: LeaseClaim,
        config: RoleServiceConfig,
        *,
        abort_engine: Callable[[], bool],
    ) -> None:
        self._queues = queues
        self._claim = claim
        self._config = config
        self._abort_engine = abort_engine
        self._stop = Event()
        self._wake = Event()
        self._lock = Lock()
        self._turn: WorkerTurn | None = None
        self._turn_deadline: float | None = None
        self.error: Exception | None = None
        self.failure_reason: str | None = None
        self._thread = Thread(target=self._run, name=f"lease-{claim.lease_id}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def watch(self, turn: WorkerTurn) -> None:
        with self._lock:
            self._turn = turn
            self._turn_deadline = monotonic() + self._config.turn_timeout_seconds
        self._wake.set()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=max(5, self._config.heartbeat_seconds + 1))
        if self._thread.is_alive() and self.error is None:
            self.error = RoleServiceError("lease heartbeat did not stop")

    def _run(self) -> None:
        next_heartbeat = monotonic() + self._config.heartbeat_seconds
        while not self._stop.is_set():
            with self._lock:
                deadline = self._turn_deadline
            wake_at = next_heartbeat if deadline is None else min(next_heartbeat, deadline)
            self._wake.wait(max(0.0, wake_at - monotonic()))
            self._wake.clear()
            if self._stop.is_set():
                return
            now = monotonic()
            with self._lock:
                deadline = self._turn_deadline
                turn = self._turn
            if deadline is not None and now >= deadline:
                self.error = RoleServiceError("provider turn timed out")
                self.failure_reason = "provider-turn-timeout"
                if turn is not None:
                    try:
                        turn.interrupt()
                    except Exception:
                        pass
                try:
                    self._abort_engine()
                except Exception:
                    pass
                return
            if now < next_heartbeat:
                continue
            try:
                self._queues.heartbeat(
                    project_id=self._claim.project_id,
                    lease_id=self._claim.lease_id,
                    lease_token=self._claim.lease_token,
                    lease_seconds=self._config.lease_seconds,
                )
                next_heartbeat = monotonic() + self._config.heartbeat_seconds
            except Exception as exc:
                self.error = exc
                self.failure_reason = "lease-heartbeat-failed"
                if turn is not None:
                    try:
                        turn.interrupt()
                    except Exception:
                        pass
                return


class RoleService:
    def __init__(
        self,
        database_url: str,
        config: RoleServiceConfig,
        *,
        provider_factory: Callable[[RoleInstanceKey], WorkerProvider] | None = None,
        workspace_resolver: Callable[[str, str, str], Path] | None = None,
        prompt_resolver: Callable[[str, str, str], RenderedRoleStatePrompt] | None = None,
    ) -> None:
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise DatabaseConfigurationError("the V5 database URL must use Postgres")
        if not isinstance(config, RoleServiceConfig):
            raise RoleServiceConfigurationError("role service configuration is invalid")
        self._database_url = database_url
        self.config = config
        self._sandbox_policy = _codex_sandbox_policy()
        self._queues = RoleQueueStore(database_url)
        selected_factory = provider_factory or self._codex_provider
        self._pool = WarmEnginePool(selected_factory)
        self._affinity_store = ThreadAffinityStore(database_url)
        self._affinity = ThreadAffinityCoordinator(self._affinity_store, self._pool)
        self._workspace_resolver = workspace_resolver or self._prepare_workspace
        self._prompt_resolver = prompt_resolver or (
            lambda role, flow, state: render_role_state_prompt(
                config.configuration_root, role, flow, state
            )
        )
        if not callable(self._workspace_resolver) or not callable(self._prompt_resolver):
            raise RoleServiceConfigurationError("role service resolver is invalid")
        self._sources = _load_source_bindings(
            config.source_repositories_file, config.project_id
        )

    def close(self) -> None:
        self._pool.shutdown()

    def run_once(self) -> RoleServiceResult:
        claim = self._queues.claim(
            project_id=self.config.project_id,
            queue_id=self.config.role_id,
            owner_instance_id=self.config.instance_id,
            lease_seconds=self.config.lease_seconds,
        )
        if claim is None:
            return self._result("empty")
        engine_key = RoleInstanceKey(
            self.config.project_id,
            self.config.instance_id,
        )
        heartbeat = _LeaseHeartbeat(
            self._queues,
            claim,
            self.config,
            abort_engine=lambda: self._pool.abort_active(engine_key),
        )
        heartbeat.start()
        reason = "turn-failed"
        try:
            try:
                retry_recorded = self._retry_attempt_already_recorded(
                    claim.queue_item.work_item_id,
                    claim.queue_item.payload,
                )
            except RoleServiceError as exc:
                reason = str(exc)
                return self._release(claim, reason)
            if retry_recorded:
                self._queues.complete(
                    project_id=claim.project_id,
                    lease_id=claim.lease_id,
                    lease_token=claim.lease_token,
                )
                return self._result(
                    "completed",
                    claim=claim,
                    reason="reliability-attempt-already-recorded",
                )
            context = self._work_context(claim.queue_item.work_item_id)
            prompt = self._prompt_resolver(
                context.role_reference, context.flow_reference, context.state_id
            )
            if prompt.digest != context.prompt_digest:
                reason = "prompt-digest-mismatch"
                return self._release(claim, reason)
            workspace = self._workspace_resolver(
                self.config.project_id,
                claim.queue_item.work_item_id,
                self.config.instance_id,
            )
            if (
                not isinstance(workspace, Path)
                or not workspace.is_absolute()
                or not workspace.is_dir()
            ):
                reason = "workspace-unavailable"
                return self._release(claim, reason)
            payload_text = json.dumps(
                claim.queue_item.payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            reject_sensitive_content(payload_text, "queue payload")
            before = self._progress_cursor(claim.queue_item.work_item_id)
            conversation = _conversation_id(claim.queue_item.payload)
            key = ThreadAffinityKey(
                self.config.project_id,
                claim.queue_item.work_item_id,
                self.config.role_id,
                conversation,
            )
            self._affinity_store.release_superseded_operation(
                key,
                instance_id=self.config.instance_id,
                prompt_digest=prompt.digest,
                superseded_before=claim.acquired_at,
            )
            completed = self._affinity.run(
                key,
                instance_id=self.config.instance_id,
                prompt_digest=prompt.digest,
                request=ThreadRequest(
                    cwd=workspace,
                    base_instructions=prompt.text,
                    developer_instructions=_operational_instructions(),
                    sandbox=self._sandbox_policy,
                ),
                operation=lambda thread: self._run_turn(
                    thread, heartbeat, claim, payload_text
                ),
            )
            heartbeat.close()
            if heartbeat.error is not None:
                reason = heartbeat.failure_reason or "lease-heartbeat-failed"
                return self._release(claim, reason)
            after = self._progress_cursor(claim.queue_item.work_item_id)
            if not completed:
                reason = "provider-not-completed"
                return self._release(claim, reason)
            if after <= before:
                reason = "no-durable-effect"
                return self._release(claim, reason)
            self._queues.complete(
                project_id=claim.project_id,
                lease_id=claim.lease_id,
                lease_token=claim.lease_token,
            )
            return self._result(
                "completed",
                claim=claim,
                reason="durable-effect-recorded",
            )
        except (KeyboardInterrupt, SystemExit):
            self._try_release(claim)
            raise
        except Exception:
            self._try_release(claim)
            return self._result("released", claim=claim, reason=reason)
        finally:
            heartbeat.close()

    def run_forever(self, *, stop: Event | None = None) -> None:
        selected = stop or Event()
        try:
            while not selected.is_set():
                result = self.run_once()
                if result.status in {"empty", "released"}:
                    selected.wait(self.config.poll_seconds)
        finally:
            self.close()

    def _run_turn(
        self,
        thread: WorkerThread,
        heartbeat: _LeaseHeartbeat,
        claim: LeaseClaim,
        payload_text: str,
    ) -> bool:
        turn = thread.start_turn(
            TurnRequest(prompt=_turn_prompt(claim, payload_text))
        )
        heartbeat.watch(turn)
        completed = False
        for event in turn.events():
            if heartbeat.error is not None:
                return False
            if event.kind is ProviderEventKind.ERROR:
                return False
            if event.kind is ProviderEventKind.TURN_COMPLETED:
                completed = event.completion is TurnCompletionStatus.COMPLETED
        return completed

    def _work_context(self, work_item_id: str) -> _WorkContext:
        try:
            with psycopg.connect(
                self._database_url, autocommit=True
            ) as connection:
                row = connection.execute(
                    f"""
                    SELECT binding.role_reference, binding.flow_reference,
                           binding.prompt_configuration_digest, run.current_state
                    FROM {SCHEMA}.role_bindings AS binding
                    JOIN {SCHEMA}.flow_runs AS run
                      ON run.project_id = binding.project_id
                    JOIN {SCHEMA}.work_items AS work
                      ON work.project_id = run.project_id
                     AND work.work_item_id = run.work_item_id
                    WHERE binding.project_id = %s AND binding.role_id = %s
                      AND run.work_item_id = %s
                      AND work.status NOT IN ('completed', 'error')
                    """,
                    (self.config.project_id, self.config.role_id, work_item_id),
                ).fetchone()
        except Exception as exc:
            raise RoleServiceError(_SAFE_FAILURE) from exc
        if row is None:
            raise RoleServiceError("active role flow context is unavailable")
        return _WorkContext(*row)

    def _retry_attempt_already_recorded(
        self,
        work_item_id: str,
        payload: Mapping[str, object],
    ) -> bool:
        reliability = payload.get("reliability")
        if reliability is None:
            return False
        if not isinstance(reliability, Mapping):
            raise RoleServiceError("queue reliability metadata is invalid")
        resumed = reliability.get("resumed_after_recovery")
        if resumed is True:
            return False
        if resumed is not False:
            raise RoleServiceError("queue reliability metadata is invalid")
        incident_id = reliability.get("incident_id")
        stage = reliability.get("stage")
        attempt_number = reliability.get("attempt_number")
        if (
            not isinstance(incident_id, str)
            or _ID.fullmatch(incident_id) is None
            or stage not in {"technical", "pm_correction", "recovery"}
            or isinstance(attempt_number, bool)
            or not isinstance(attempt_number, int)
            or not 1 <= attempt_number <= (1 if stage == "recovery" else 3)
        ):
            raise RoleServiceError("queue reliability metadata is invalid")
        try:
            with psycopg.connect(
                self._database_url, autocommit=True
            ) as connection:
                return connection.execute(
                    f"""
                    SELECT EXISTS (
                        SELECT 1
                        FROM {SCHEMA}.failure_attempts AS attempt
                        JOIN {SCHEMA}.failure_incidents AS incident
                          ON incident.project_id = attempt.project_id
                         AND incident.incident_id = attempt.incident_id
                        WHERE attempt.project_id = %s
                          AND attempt.incident_id = %s
                          AND incident.work_item_id = %s
                          AND attempt.stage = %s
                          AND attempt.stage_attempt = %s
                    )
                    """,
                    (
                        self.config.project_id,
                        incident_id,
                        work_item_id,
                        stage,
                        attempt_number,
                    ),
                ).fetchone()[0]
        except Exception as exc:
            raise RoleServiceError(_SAFE_FAILURE) from exc

    def _progress_cursor(self, work_item_id: str) -> int:
        try:
            with psycopg.connect(
                self._database_url, autocommit=True
            ) as connection:
                return int(
                    connection.execute(
                        f"SELECT COALESCE(max(progress_id), 0) FROM {SCHEMA}.progress "
                        "WHERE project_id = %s AND work_item_id = %s "
                        "AND role_instance_id = %s",
                        (
                            self.config.project_id,
                            work_item_id,
                            self.config.instance_id,
                        ),
                    ).fetchone()[0]
                )
        except Exception as exc:
            raise RoleServiceError(_SAFE_FAILURE) from exc

    def _prepare_workspace(self, project_id: str, work_item_id: str, actor_id: str) -> Path:
        workspace = ProjectRegistrationCoordinator(
            self._database_url,
            configuration_root=self.config.configuration_root,
        ).prepare_workspace(
            project_id=project_id,
            work_item_id=work_item_id,
            actor_id=actor_id,
            source_repositories=self._sources,
        )
        selected = next(
            (item for item in workspace.repositories if item.repository_id == "primary"),
            workspace.repositories[0] if workspace.repositories else None,
        )
        if selected is None:
            raise RoleServiceError("workspace has no repository")
        return Path(selected.worktree_path)

    def _codex_provider(self, _key: RoleInstanceKey) -> WorkerProvider:
        home = os.environ.get("CODEX_HOME", "").strip()
        if not home:
            raise RoleServiceConfigurationError("CODEX_HOME is required")
        selected: dict[str, str] = {"CODEX_HOME": home}
        for name in (
            "AGENTIC_MESH_V5_API_URL",
            "AGENTIC_MESH_V5_API_TOKEN_FILE",
            "AGENTIC_MESH_V5_CONFIG_ROOT",
        ):
            value = os.environ.get(name, "").strip()
            if value:
                selected[name] = value
        return CodexWorkerProvider(CodexProviderConfig(environment=selected))

    def _release(self, claim: LeaseClaim, reason: str) -> RoleServiceResult:
        self._try_release(claim)
        return self._result("released", claim=claim, reason=reason)

    def _try_release(self, claim: LeaseClaim) -> None:
        try:
            self._queues.release(
                project_id=claim.project_id,
                lease_id=claim.lease_id,
                lease_token=claim.lease_token,
            )
        except Exception:
            pass

    def _result(
        self,
        status: Literal["empty", "completed", "released"],
        *,
        claim: LeaseClaim | None = None,
        reason: str | None = None,
    ) -> RoleServiceResult:
        return RoleServiceResult(
            status,
            self.config.project_id,
            self.config.role_id,
            self.config.instance_id,
            None if claim is None else claim.queue_item.queue_item_id,
            None if claim is None else claim.queue_item.work_item_id,
            reason,
        )


def _load_source_bindings(path: Path, project_id: str) -> Mapping[str, Path]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        projects = payload["projects"]
        repositories = projects[project_id]["repositories"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
        raise RoleServiceConfigurationError("source repository bindings are invalid") from None
    if not isinstance(repositories, dict) or not repositories:
        raise RoleServiceConfigurationError("source repository bindings are invalid")
    result: dict[str, Path] = {}
    for key, value in repositories.items():
        if not isinstance(key, str) or _ID.fullmatch(key) is None or not isinstance(value, str):
            raise RoleServiceConfigurationError("source repository bindings are invalid")
        selected = Path(value)
        if not selected.is_absolute() or not selected.is_dir():
            raise RoleServiceConfigurationError("source repository binding is unavailable")
        result[key] = selected
    return result


def _conversation_id(payload: Mapping[str, object]) -> str:
    value = payload.get("conversation_id", "work")
    if not isinstance(value, str) or _CONVERSATION.fullmatch(value) is None:
        raise RoleServiceError("queue conversation identity is invalid")
    return value


def _operational_instructions() -> str:
    return (
        "You are running as one Agentic Mesh V5 role service. The queue item is "
        "durable work, not a chat request. Inspect current state through the V5 "
        "control API and use `python -m agentic_mesh_v5 control-call` for durable "
        "progress, decisions, routing, handoffs and evidence. Record a structured "
        "progress checkpoint for every material action. Do not expose private "
        "reasoning, credentials, token values or raw provider output. Do not mark "
        "work complete unless the configured lifecycle and handoff rules are met. "
        "Keep the solution simple and ask the sponsor when material intent is ambiguous."
    )


def _turn_prompt(claim: LeaseClaim, payload_text: str) -> str:
    return (
        "Process this accepted durable queue item.\n"
        f"Project: {claim.project_id}\n"
        f"Work item: {claim.queue_item.work_item_id}\n"
        f"Queue item: {claim.queue_item.queue_item_id}\n"
        f"Attempt: {claim.queue_item.attempt_count}\n"
        f"Payload: {payload_text}\n"
        "Before ending the turn, record a new structured progress checkpoint for this "
        "role instance after every legitimate durable effect, including a routed or "
        "escalated blocker. The queue item is acknowledged only after that checkpoint."
    )
