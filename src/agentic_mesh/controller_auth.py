from __future__ import annotations

import html
import json
import mimetypes
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from threading import Thread
from typing import Any
from urllib.parse import parse_qs
from urllib.parse import quote
from urllib.parse import unquote
from urllib.parse import urlencode
from urllib.parse import urlparse
from uuid import uuid4

from agentic_mesh import telemetry
from agentic_mesh import current_agent_status
from agentic_mesh import status_dashboard
from agentic_mesh.activation_evidence import ActivationReadError
from agentic_mesh.activation_evidence import FileActivationEvidenceStore
from agentic_mesh.human_gates import derive_human_gate_summary
from agentic_mesh.human_gates import FileHumanGateRequestStore
from agentic_mesh.human_response_submissions import HumanResponseSubmissionService
from agentic_mesh.config import load_mesh_config
from agentic_mesh.journal import EventJournal
from agentic_mesh.models import AuthCredential
from agentic_mesh.models import AuthMethod
from agentic_mesh.models import MeshConfig
from agentic_mesh.models import new_id
from agentic_mesh.problem_status import ProblemStatusStore
from agentic_mesh.route_status import CurrentRouteStore
from agentic_mesh.storage import FileMessageStore
from agentic_mesh.work_item_recovery import DuplicateActiveWorkGuard
from agentic_mesh.work_item_recovery import FileRecoveryStatusStore
from agentic_mesh.work_item_recovery import RecoveryActionRequest
from agentic_mesh.work_item_recovery import RecoveryActionService
from agentic_mesh.workers import codex_auth_failure_reason
from agentic_mesh.worker_runs import FileWorkerRunStore
from agentic_mesh.worker_runs import WorkerRun
from agentic_mesh.worker_runs import WorkerRunReadError


@dataclass
class OAuthLoginSession:
    session_id: str
    credential_id: str
    codex_home: Path
    started_at: float
    output: list[str] = field(default_factory=list)
    returncode: int | None = None
    error: str | None = None
    login_url: str | None = None
    user_code: str | None = None

    @property
    def status(self) -> str:
        if self.error:
            return "failed"
        if self.returncode is None:
            return "running"
        if self.returncode == 0:
            return "completed"
        return "failed"


class ControllerAuthService:
    def __init__(
        self,
        *,
        config_root: Path,
        project_file: str,
        state_root: Path,
        workspace_root: Path | None = None,
    ) -> None:
        self.config_root = config_root
        self.project_file = project_file
        self.state_root = state_root
        self.workspace_root = workspace_root or config_root
        self.secret_root = state_root / "secrets"
        self.mount_root = state_root / "worker_mounts"
        self._sessions: dict[str, OAuthLoginSession] = {}
        self._lock = Lock()
        self._mesh_config_cache: MeshConfig | None = None
        self._journal_events_cache: dict[str, list[dict[str, Any]]] | None = None

    def load_config(self) -> MeshConfig:
        if self._mesh_config_cache is None:
            self._mesh_config_cache = load_mesh_config(
                self.config_root,
                project_file=self.project_file,
            )
        return self._mesh_config_cache

    def credential_statuses(self) -> list[dict[str, Any]]:
        mesh_config = self.load_config()
        roles_by_credential: dict[str, list[str]] = {}
        for role_id, role in mesh_config.project.roles.items():
            auth = role.worker.auth
            if auth and auth.credential_ref:
                roles_by_credential.setdefault(auth.credential_ref, []).append(role_id)

        statuses = []
        for credential_id, credential in sorted(
            mesh_config.project.auth_credentials.items()
        ):
            method = mesh_config.auth_methods[credential.method]
            statuses.append(
                self.credential_status(
                    credential,
                    method,
                    roles=sorted(roles_by_credential.get(credential_id, [])),
                )
            )
        return statuses

    def credential_status(
        self,
        credential: AuthCredential,
        method: AuthMethod,
        *,
        roles: list[str] | None = None,
    ) -> dict[str, Any]:
        status = "configured"
        detail = "Credential reference is configured."
        secret_path = (
            self.secret_root / credential.secret_ref
            if credential.secret_ref is not None
            else None
        )
        mount_path = (
            self.mount_root / credential.mount_ref
            if credential.mount_ref is not None
            else None
        )

        if method.requires_secret_ref:
            if secret_path is None or not secret_path.exists():
                status = "missing"
                detail = "Secret file is missing."
            elif not secret_path.read_text(encoding="utf-8").strip():
                status = "missing"
                detail = "Secret file is empty."
            else:
                status = "configured"
                detail = "Secret file exists and is non-empty."
        elif method.method_id == "codex_oauth_cache":
            status, detail = self._codex_oauth_status(credential, mount_path)
        elif method.requires_mount_ref:
            if mount_path is None or not mount_path.exists():
                status = "missing"
                detail = "Credential mount is missing."
            else:
                status = "configured"
                detail = "Credential mount exists."

        return {
            "credential": credential.credential_id,
            "method": credential.method,
            "category": method.category,
            "status": status,
            "detail": detail,
            "secret_ref": credential.secret_ref,
            "mount_ref": credential.mount_ref,
            "roles": roles or [],
            "redacted": True,
        }

    def _codex_oauth_status(
        self,
        credential: AuthCredential,
        mount_path: Path | None,
    ) -> tuple[str, str]:
        if mount_path is None:
            return "missing", "OAuth credential has no mount_ref."
        if not mount_path.exists():
            return "missing", "OpenAI sign-in has not been completed."
        codex_bin = shutil.which("codex")
        if not codex_bin:
            auth_json = mount_path / "auth.json"
            if auth_json.exists():
                return "unknown", "OpenAI sign-in appears to be configured."
            return "missing", "OpenAI sign-in has not been completed."
        env = os.environ.copy()
        env.update(credential.env)
        env["CODEX_HOME"] = str(mount_path)
        try:
            completed = subprocess.run(
                [codex_bin, "login", "status"],
                capture_output=True,
                text=True,
                env=env,
                timeout=10,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return "unknown", "Codex login status timed out."
        output = "\n".join(
            part.strip()
            for part in [completed.stdout, completed.stderr]
            if part and part.strip()
        )
        if completed.returncode == 0:
            return self._codex_oauth_live_status(
                codex_bin,
                credential,
                mount_path,
                login_detail=output,
            )
        auth_reason = codex_auth_failure_reason(output)
        if auth_reason:
            return "auth_failed", auth_reason
        return "missing", output or "OpenAI sign-in has not been completed."

    def _codex_oauth_live_status(
        self,
        codex_bin: str,
        credential: AuthCredential,
        mount_path: Path,
        *,
        login_detail: str,
    ) -> tuple[str, str]:
        env = os.environ.copy()
        env.update(credential.env)
        env["CODEX_HOME"] = str(mount_path)
        cwd = self.workspace_root if self.workspace_root.exists() else self.config_root
        try:
            completed = subprocess.run(
                [
                    codex_bin,
                    "exec",
                    "--skip-git-repo-check",
                    "--sandbox",
                    "danger-full-access",
                    "-c",
                    "model_reasoning_effort=none",
                    "Reply with exactly OK.",
                ],
                capture_output=True,
                text=True,
                env=env,
                cwd=cwd,
                timeout=30,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return "unknown", "Codex live authentication check timed out."
        output = "\n".join(
            part.strip()
            for part in [completed.stdout, completed.stderr]
            if part and part.strip()
        )
        if completed.returncode == 0:
            return "configured", "Live Codex check passed."
        auth_reason = codex_auth_failure_reason(output)
        if auth_reason:
            return "auth_failed", auth_reason
        detail = output or login_detail or "Codex live authentication check failed."
        return "unknown", detail

    def store_secret(
        self,
        credential_id: str,
        secret_value: str,
        *,
        overwrite: bool,
    ) -> dict[str, Any]:
        mesh_config = self.load_config()
        credential = self._credential(mesh_config, credential_id)
        method = mesh_config.auth_methods[credential.method]
        if not method.requires_secret_ref or not credential.secret_ref:
            raise ValueError(f"Credential `{credential_id}` does not use secret_ref.")
        secret_value = secret_value.strip()
        if not secret_value:
            raise ValueError("Secret value must not be empty.")
        secret_path = self.secret_root / credential.secret_ref
        if secret_path.exists() and not overwrite:
            raise FileExistsError(
                f"Secret `{credential.secret_ref}` already exists."
            )
        secret_path.parent.mkdir(parents=True, exist_ok=True)
        secret_path.write_text(secret_value, encoding="utf-8")
        try:
            secret_path.chmod(0o600)
        except OSError:
            pass
        return {
            "credential": credential.credential_id,
            "method": credential.method,
            "secret_ref": credential.secret_ref,
            "status": "stored",
            "redacted": True,
        }

    def start_codex_oauth_login(self, credential_id: str) -> OAuthLoginSession:
        mesh_config = self.load_config()
        credential = self._credential(mesh_config, credential_id)
        method = mesh_config.auth_methods[credential.method]
        if method.method_id != "codex_oauth_cache" or not credential.mount_ref:
            raise ValueError(
                f"Credential `{credential_id}` is not a codex_oauth_cache credential."
            )
        codex_bin = shutil.which("codex")
        if not codex_bin:
            raise FileNotFoundError("Codex CLI is not installed or not on PATH.")

        codex_home = self.mount_root / credential.mount_ref
        codex_home.mkdir(parents=True, exist_ok=True)
        session = OAuthLoginSession(
            session_id=f"oauth-{uuid4().hex}",
            credential_id=credential.credential_id,
            codex_home=codex_home,
            started_at=time.time(),
        )
        with self._lock:
            self._sessions[session.session_id] = session
        thread = Thread(
            target=self._run_codex_oauth_login,
            args=(session.session_id, credential, codex_bin, codex_home),
            daemon=True,
        )
        thread.start()
        return session

    def _run_codex_oauth_login(
        self,
        session_id: str,
        credential: AuthCredential,
        codex_bin: str,
        codex_home: Path,
    ) -> None:
        env = os.environ.copy()
        env.update(credential.env)
        env["CODEX_HOME"] = str(codex_home)
        try:
            process = subprocess.Popen(
                [codex_bin, "login", "--device-auth"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
            )
            assert process.stdout is not None
            for line in process.stdout:
                self._append_session_output(session_id, line.rstrip())
            returncode = process.wait()
            with self._lock:
                self._sessions[session_id].returncode = returncode
        except Exception as exc:
            with self._lock:
                session = self._sessions[session_id]
                session.error = str(exc)
                session.returncode = 1

    def _append_session_output(self, session_id: str, line: str) -> None:
        if not line:
            return
        with self._lock:
            session = self._sessions[session_id]
            session.output.append(line)
            url = _first_url(line)
            if url:
                session.login_url = url
            code = _first_device_code(line)
            if code:
                session.user_code = code
            if len(session.output) > 200:
                session.output = session.output[-200:]

    def session(self, session_id: str) -> OAuthLoginSession | None:
        with self._lock:
            return self._sessions.get(session_id)

    def session_payload(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            return {
                "session_id": session.session_id,
                "credential_id": session.credential_id,
                "status": session.status,
                "login_url": session.login_url,
                "user_code": session.user_code,
                "output": "\n".join(session.output),
                "error": session.error,
                "redacted": True,
            }

    def work_item_status(self, work_item_id: str) -> dict[str, Any]:
        mesh_config = self.load_config()
        project_id = mesh_config.project.project_id
        claim_lease_seconds = self.claim_lease_seconds()
        events = [
            event
            for event in self._journal_events(project_id)
            if event.get("work_item_id") == work_item_id
        ]
        queue_entries = self._queue_entries(project_id, work_item_id)
        latest_completed_by_message = {
            event.get("message_id"): event
            for event in events
            if event.get("event_type") == "work_completed" and event.get("message_id")
        }
        active_claims = [
            entry
            for entry in queue_entries
            if entry["queue_state"] == "claimed"
            and entry["message_id"] not in latest_completed_by_message
        ]
        pending = [
            entry for entry in queue_entries if entry["queue_state"] == "pending"
        ]

        current = self._current_work_item_state(
            events,
            active_claims,
            pending,
            claim_lease_seconds=claim_lease_seconds,
        )
        problem_status = ProblemStatusStore(
            self.state_root,
            project_id,
        ).read_current(work_item_id)
        current_route = CurrentRouteStore(
            self.state_root,
            project_id,
        ).read_current(work_item_id)
        activation_record = FileActivationEvidenceStore(
            self.state_root,
            project_id,
            create_dirs=False,
        ).read_current_with_error(work_item_id)
        if isinstance(activation_record, ActivationReadError):
            activation_evidence = activation_record.to_summary()
            activation_summary = activation_record.to_summary()
        elif activation_record is not None:
            activation_evidence = activation_record.to_dict()
            activation_summary = activation_record.to_summary()
        else:
            activation_evidence = None
            activation_summary = None
        worker_runs = self._worker_runs(project_id, work_item_id)
        if problem_status:
            current = {
                "status": problem_status.get("status"),
                "role_id": problem_status.get("affected_role"),
                "role_instance_id": problem_status.get("role_instance_id"),
                "lifecycle_state": problem_status.get("lifecycle_state"),
                "message_id": problem_status.get("source_message_id"),
                "since": problem_status.get("occurred_at"),
                "problem_kind": problem_status.get("problem_kind"),
                "failure_class": problem_status.get("failure_class"),
                "reason_summary": problem_status.get("reason_summary"),
                "next_action": problem_status.get("next_action"),
                "action_owner": problem_status.get("action_owner"),
                "retryable": problem_status.get("retryable"),
            }
        artifact_paths = sorted(
            {
                str(event.get("path"))
                for event in events
                if event.get("event_type") == "documentation_updated"
                and event.get("path")
            }
        )
        for event in events:
            for path in event.get("artifact_paths") or []:
                if path:
                    artifact_paths.append(str(path))
        artifacts = sorted(
            set(artifact_paths)
            | set(self._work_item_prompt_audit_artifacts(mesh_config, work_item_id))
        )
        if current.get("status") == "not_found" and artifacts:
            current = {
                "status": "observed",
                "role_id": None,
                "role_instance_id": None,
                "lifecycle_state": None,
                "message_id": None,
                "since": None,
                "reason_summary": (
                    "No lifecycle or queue status was recorded, but debug "
                    "artifacts exist for this work item."
                ),
            }
        verification_by_path = {
            str(record.get("path")): record
            for record in (problem_status or {}).get("artifact_verification", [])
            if record.get("path")
        }
        artifact_records = [
            {
                "path": artifact_path,
                **_artifact_label_record(
                    artifact_path,
                    verification_by_path.get(artifact_path),
                ),
                "exists": self.resolve_artifact_path(artifact_path) is not None,
            }
            for artifact_path in artifacts
        ]
        teams_messages = [
            {
                "timestamp": event.get("timestamp"),
                "channel": event.get("channel"),
                "message_type": event.get("message_type"),
                "teams_activity_id": event.get("teams_activity_id"),
                "teams_conversation_id": event.get("teams_conversation_id"),
            }
            for event in events
            if event.get("teams_activity_id")
        ]
        queue_entries = sorted(
            queue_entries,
            key=lambda entry: str(entry.get("created_at") or ""),
        )
        title, summary = self._work_item_metadata(work_item_id, queue_entries)
        human_gate_summary = derive_human_gate_summary(
            mesh_config=mesh_config,
            state_root=self.state_root,
            work_item_id=work_item_id,
            current=current,
            problem_status=problem_status,
        )
        recovery_status = FileRecoveryStatusStore(
            self.state_root,
            project_id,
            create_dirs=False,
        ).get_current(work_item_id)
        recovery_status_payload = (
            recovery_status.to_dict() if recovery_status is not None else None
        )
        live_human_gate_active = (
            current.get("status") == "waiting_for_human_response"
            and human_gate_summary.get("status")
            in {"waiting_for_response", "pending"}
        )
        notification_state = self._notification_state(events)
        if live_human_gate_active:
            # Historical recovery and failed problem-notification records can
            # outlive the runtime issue they described. A live human gate is the
            # current unblock path, so keep old evidence in the timeline but do
            # not present it as the active status problem.
            recovery_status_payload = None
            notification_state = {
                "status": "superseded_by_human_gate",
                "updated_at": human_gate_summary.get("requested_at"),
            }
        unblock_guidance = self._work_item_unblock_guidance(
            work_item_id=work_item_id,
            current=current,
            human_gate_summary=human_gate_summary,
            problem_status=problem_status,
            recovery_status=recovery_status_payload,
        )
        return {
            "project_id": project_id,
            "work_item_id": work_item_id,
            "title": title,
            "summary": summary,
            "status": current["status"],
            "current": current,
            "problem_status": problem_status,
            "activation_evidence": activation_evidence,
            "activation_summary": activation_summary,
            "worker_runs": worker_runs,
            "human_gate_summary": human_gate_summary,
            "recovery_status": recovery_status_payload,
            "unblock_guidance": unblock_guidance,
            "current_route": current_route,
            "notification_state": notification_state,
            "counts": {
                "events": len(events),
                "pending": len(pending),
                "claimed": len(active_claims),
                "completed": len(
                    [
                        event
                        for event in events
                        if event.get("event_type") == "work_completed"
                    ]
                ),
            },
            "claim_lease_seconds": claim_lease_seconds,
            "queue_entries": queue_entries,
            "artifacts": artifacts,
            "artifact_records": artifact_records,
            "missing_artifacts": [
                record["path"] for record in artifact_records if not record["exists"]
            ],
            "teams_messages": teams_messages,
            "timeline": events,
        }

    def _work_item_unblock_guidance(
        self,
        *,
        work_item_id: str,
        current: dict[str, Any],
        human_gate_summary: dict[str, Any],
        problem_status: dict[str, Any] | None,
        recovery_status: dict[str, Any] | None,
    ) -> dict[str, Any]:
        response_request_id = (
            human_gate_summary.get("response_request_id")
            or human_gate_summary.get("approval_request_id")
        )
        current_gate = human_gate_summary.get("current_human_gate") or {}
        lifecycle_state = str(
            current_gate.get("lifecycle_state")
            or current.get("lifecycle_state")
            or ""
        )
        gate_id = str(current_gate.get("gate_id") or "")
        revision = int((recovery_status or {}).get("revision") or 0)
        actor = "sponsor"
        if response_request_id and lifecycle_state and gate_id:
            return {
                "state": "live_human_response_required",
                "label": "Waiting for your response",
                "summary": (
                    "This work item has an active human gate. Respond from the "
                    "Teams approval card if available, or record the response "
                    "through the CLI command below."
                ),
                "can_user_answer_now": True,
                "action_owner": "sponsor",
                "available_actions": [
                    {
                        "label": "Approve through CLI",
                        "action": "record_human_response",
                        "command": (
                            "python -m agentic_mesh.cli record-human-response "
                            f"--work-item-id {work_item_id} "
                            f"--lifecycle-state {lifecycle_state} "
                            f"--gate-id {gate_id} "
                            f"--response-request-id {response_request_id} "
                            "--responder sponsor --value approve"
                        ),
                    },
                    {
                        "label": "Reject through CLI",
                        "action": "record_human_response",
                        "command": (
                            "python -m agentic_mesh.cli record-human-response "
                            f"--work-item-id {work_item_id} "
                            f"--lifecycle-state {lifecycle_state} "
                            f"--gate-id {gate_id} "
                            f"--response-request-id {response_request_id} "
                            "--responder sponsor --value reject"
                        ),
                    },
                ],
            }

        recovery_state = str((recovery_status or {}).get("recovery_state") or "")
        recoverability = str(
            (recovery_status or {}).get("recoverability_class") or ""
        )
        problem_status_value = str((problem_status or {}).get("status") or "")
        problem_summary = str(
            (problem_status or {}).get("reason_summary")
            or (problem_status or {}).get("reason")
            or "No detailed reason captured."
        )
        if recovery_state == "runtime_fix_required" or recoverability == "fix_runtime_first":
            return {
                "state": "operator_recovery_required",
                "label": "Runtime fix required before retry",
                "summary": (
                    "This is not waiting for sponsor input. The runtime or "
                    f"worker failed and must be repaired first. Last reason: {problem_summary}"
                ),
                "can_user_answer_now": False,
                "action_owner": "runtime/operator",
                "available_actions": [
                    {
                        "label": "Mark repaired and recover",
                        "action": "recover",
                        "command": (
                            "python -m agentic_mesh.cli work-item recovery recover "
                            f"--work-item-id {work_item_id} "
                            f"--expected-revision {revision} "
                            "--actor operator "
                            "--reason \"Runtime repair confirmed\" "
                            f"--idempotency-key recover-{work_item_id}-{revision} "
                            "--repair-confirmed"
                        ),
                    }
                ],
            }

        if (
            recovery_state == "sponsor_decision_required"
            or problem_status_value == "blocked"
        ):
            return {
                "state": "historical_blocker_unanswerable",
                "label": "Historical blocker has no live response request",
                "summary": (
                    "This item was backfilled as needing a sponsor decision, "
                    "but no active human-gate request or concrete question was "
                    "preserved. It cannot be answered in-place; retry it if it "
                    "is still useful, or supersede it with a clearer work item."
                ),
                "can_user_answer_now": False,
                "action_owner": str(
                    (problem_status or {}).get("action_owner") or "sponsor"
                ),
                "available_actions": [
                    {
                        "label": "Retry this work item",
                        "action": "retry",
                        "command": (
                            "python -m agentic_mesh.cli work-item recovery retry "
                            f"--work-item-id {work_item_id} "
                            f"--expected-revision {revision} "
                            f"--actor {actor} "
                            "--reason \"Retry after sponsor review\" "
                            f"--idempotency-key retry-{work_item_id}-{revision}"
                        ),
                    },
                    {
                        "label": "Supersede with replacement work item",
                        "action": "supersede",
                        "requires_replacement_work_item_id": True,
                        "command": (
                            "python -m agentic_mesh.cli work-item recovery supersede "
                            f"--work-item-id {work_item_id} "
                            "--replacement-work-item-id <replacement-work-item-id> "
                            f"--expected-revision {revision} "
                            f"--actor {actor} "
                            "--reason \"Superseded by replacement work\" "
                            f"--idempotency-key supersede-{work_item_id}-{revision}"
                        ),
                    },
                ],
            }

        if problem_status or recovery_status:
            return {
                "state": "review_status",
                "label": "Review recovery status",
                "summary": (
                    "This work item has recovery/problem state, but no direct "
                    "human response is currently available. Review the current "
                    "problem and recovery details before retrying or closing it."
                ),
                "can_user_answer_now": False,
                "action_owner": str(
                    (problem_status or {}).get("action_owner")
                    or (recovery_status or {}).get("action_owner")
                    or "operator"
                ),
                "available_actions": [],
            }

        return {
            "state": "no_unblock_action",
            "label": "No unblock action needed",
            "summary": "This work item is not currently blocked by a human gate or recovery state.",
            "can_user_answer_now": False,
            "action_owner": "none",
            "available_actions": [],
        }

    def record_human_response(self, form: dict[str, str]) -> dict[str, Any]:
        required = [
            "work_item_id",
            "lifecycle_state",
            "gate_id",
            "response_request_id",
            "responder",
            "value",
        ]
        missing = [field for field in required if not form.get(field)]
        if missing:
            return {
                "accepted": False,
                "final": False,
                "duplicate": False,
                "validation_reason": "missing_required_fields",
                "missing_fields": missing,
            }

        mesh_config = self.load_config()
        project_id = mesh_config.project.project_id
        flow_state = mesh_config.project.flow.states.get(form["lifecycle_state"])
        gate = None
        if flow_state is not None:
            gate = next(
                (
                    candidate
                    for candidate in flow_state.gates
                    if candidate.gate_id == form["gate_id"]
                ),
                None,
            )
        target_role = (
            form.get("role")
            or (flow_state.owner_role if flow_state is not None else None)
            or "release-manager"
        )
        journal = EventJournal(self.state_root, project_id)
        service = HumanResponseSubmissionService(
            project_id=project_id,
            store=FileHumanGateRequestStore(self.state_root, project_id),
            message_store=FileMessageStore(self.state_root, project_id, journal),
        )
        result = service.submit(
            target_role=target_role,
            work_item_id=form["work_item_id"],
            work_item_type=form.get("work_item_type") or "slice",
            lifecycle_state=form["lifecycle_state"],
            gate_id=form["gate_id"],
            response_type=gate.response_type if gate is not None else None,
            approval_request_id=form.get("approval_request_id") or None,
            response_request_id=form["response_request_id"],
            responder=form.get("responder") or "sponsor",
            response_value=_normalize_human_response_value(
                _parse_human_response_value(form["value"]),
                gate.response_type if gate is not None else None,
            ),
            authenticated=True,
            source=form.get("source") or "cli",
            correlation_id=form.get("correlation_id") or new_id("corr"),
        )
        return result.to_dict()

    def execute_work_item_action(
        self,
        work_item_id: str,
        *,
        action: str,
        form: dict[str, str],
    ) -> dict[str, Any]:
        mesh_config = self.load_config()
        project_id = mesh_config.project.project_id
        journal = EventJournal(self.state_root, project_id)
        recovery_store = FileRecoveryStatusStore(self.state_root, project_id)
        current = recovery_store.get_current(work_item_id)
        revision = _safe_int(form.get("expected_revision")) or (
            current.revision if current else 1
        )
        actor = form.get("actor") or "operator"
        reason = form.get("reason") or _default_work_item_action_reason(action)
        problem_store = ProblemStatusStore(self.state_root, project_id)

        if action in {"retry", "recover", "supersede"}:
            action_type = {
                "retry": "retry_work_item_recovery",
                "recover": "record_work_item_recovery",
                "supersede": "supersede_work_item_recovery",
            }[action]
            request = RecoveryActionRequest(
                action_type=action_type,
                project_id=project_id,
                work_item_id=work_item_id,
                work_item_type=form.get("work_item_type") or None,
                queue_item_id=form.get("queue_item_id") or None,
                lifecycle_state=form.get("lifecycle_state") or None,
                affected_role=form.get("affected_role") or None,
                actor=actor,
                reason=reason,
                idempotency_key=(
                    form.get("idempotency_key")
                    or f"web-{action}-{work_item_id}-{revision}"
                ),
                expected_revision=revision,
                correlation_id=form.get("correlation_id") or new_id("corr"),
                source_type="controller-ui",
                repair_confirmed=action == "recover",
                replacement_work_item_id=(
                    form.get("replacement_work_item_id") or None
                ),
            )
            service = RecoveryActionService(
                project_id=project_id,
                store=recovery_store,
                message_store=FileMessageStore(self.state_root, project_id, journal),
                journal=journal,
                duplicate_guard=DuplicateActiveWorkGuard(
                    state_root=self.state_root,
                    project_id=project_id,
                ),
            )
            receipt = service.execute(request)
            if receipt.outcome in {
                "accepted",
                "queued",
                "duplicate",
                "superseded",
            }:
                problem_store.clear_current(work_item_id)
            return {
                "ok": receipt.outcome
                in {"accepted", "queued", "duplicate", "superseded"},
                "action": action,
                "receipt": receipt.to_dict(),
                "message": f"{action} recorded with outcome {receipt.outcome}.",
            }

        if action in {"cancel", "complete"}:
            if current is not None:
                recovery_store.apply_transition(
                    current,
                    expected_revision=revision,
                    recovery_state=(
                        "not_recoverable"
                        if action == "cancel"
                        else "recovery_succeeded"
                    ),
                    recoverability_class=(
                        "not_recoverable"
                        if action == "cancel"
                        else current.recoverability_class
                    ),
                    next_action=(
                        "Work item was cancelled manually."
                        if action == "cancel"
                        else "Work item was marked complete manually."
                    ),
                    action_owner="none",
                    journal_ref=(
                        "work_item_manually_cancelled"
                        if action == "cancel"
                        else "work_item_manually_completed"
                    ),
                )
            cleared_problem = problem_store.clear_current(work_item_id)
            status = "cancelled" if action == "cancel" else "completed"
            event_type = (
                "work_item_manually_cancelled"
                if action == "cancel"
                else "work_item_manually_completed"
            )
            journal.append(
                event_type,
                project_id=project_id,
                work_item_id=work_item_id,
                lifecycle_state=form.get("lifecycle_state") or None,
                role_id=form.get("affected_role") or None,
                role_instance_id="controller-ui",
                status=status,
                actor=actor,
                reason=reason,
                problem_status_cleared=cleared_problem,
                schema_version="controller-work-item-action-v0",
            )
            journal.append(
                "work_completed",
                project_id=project_id,
                work_item_id=work_item_id,
                lifecycle_state=form.get("lifecycle_state") or None,
                role_id=form.get("affected_role") or None,
                role_instance_id="controller-ui",
                status=status,
                actor=actor,
                reason=reason,
                source="controller-ui",
            )
            return {
                "ok": True,
                "action": action,
                "message": f"Work item {status} manually.",
                "problem_status_cleared": cleared_problem,
            }

        raise ValueError(f"Unsupported work item action `{action}`.")

    def _worker_runs(self, project_id: str, work_item_id: str) -> dict[str, Any]:
        store = FileWorkerRunStore(self.state_root, project_id)
        runs = store.list_recent(work_item_id=work_item_id, limit=25)
        current = []
        recent = []
        for item in runs:
            if isinstance(item, WorkerRunReadError):
                recent.append(item.safe_summary())
                continue
            if not isinstance(item, WorkerRun):
                continue
            summary = item.safe_summary()
            recent.append(summary)
            if item.run_status in {"starting", "running"}:
                current.append(summary)
        return {
            "schema_version": "worker-run-evidence-v0",
            "current": current,
            "recent": recent,
            "read_only": True,
        }

    def status_dashboard_payload(self, route_name: str) -> dict[str, Any]:
        mesh_config = self.load_config()
        with telemetry.start_span(
            "agentic_mesh.status_dashboard.read",
            attributes={
                "agentic_mesh.project_id": mesh_config.project.project_id,
                "agentic_mesh.route": route_name,
                "agentic_mesh.schema_version": status_dashboard.SCHEMA_VERSION,
            },
        ):
            previous_cache = self._journal_events_cache
            self._journal_events_cache = {}
            try:
                payload = status_dashboard.build_status_dashboard(
                    mesh_config=mesh_config,
                    state_root=self.state_root,
                    workspace_root=self.workspace_root,
                    work_item_status=self.work_item_status,
                    artifact_exists=lambda path: self.resolve_artifact_path(path) is not None,
                )
            finally:
                self._journal_events_cache = previous_cache
        telemetry.emit_log(
            {
                "event_type": "status_dashboard_read",
                "project_id": mesh_config.project.project_id,
                "route": route_name,
                "schema_version": status_dashboard.SCHEMA_VERSION,
                "work_item_count": len(payload["work_items"]),
                "queue_item_count": len(payload["queue_items"]),
                "generated_at": payload["generated_at"],
            }
        )
        return payload

    def current_agents_payload(self, route_name: str) -> dict[str, Any]:
        mesh_config = self.load_config()
        with telemetry.start_span(
            "agentic_mesh.current_agents.read",
            attributes={
                "agentic_mesh.project_id": mesh_config.project.project_id,
                "agentic_mesh.route": route_name,
                "agentic_mesh.schema_version": current_agent_status.SCHEMA_VERSION,
            },
        ):
            payload = current_agent_status.build_current_agent_status(
                mesh_config=mesh_config,
                state_root=self.state_root,
                workspace_root=self.workspace_root,
            )
        telemetry.emit_log(
            {
                "event_type": "current_agents_read",
                "project_id": mesh_config.project.project_id,
                "route": route_name,
                "schema_version": current_agent_status.SCHEMA_VERSION,
                "total_agent_count": payload["counts"]["total_agents"],
                "attention_count": payload["counts"]["attention_count"],
                "extraction_error_count": payload["counts"]["extraction_error_count"],
                "generated_at": payload["generated_at"],
            }
        )
        return payload

    def resolve_artifact_path(self, artifact_path: str) -> Path | None:
        mesh_config = self.load_config()
        artifact_path = artifact_path.strip().lstrip("/\\")
        requested = Path(artifact_path)
        if not artifact_path or requested.is_absolute() or ".." in requested.parts:
            return None
        effective_workspace_root = self._effective_workspace_root(mesh_config)
        if artifact_path.startswith("documents/analysis/"):
            root = effective_workspace_root
        else:
            root = self._document_library_root(mesh_config, effective_workspace_root)
        root = root.resolve()
        path = (root / requested).resolve()
        if path != root and root not in path.parents:
            return None
        if not path.exists() or not path.is_file():
            return None
        return path

    def _work_item_prompt_audit_artifacts(
        self,
        mesh_config: MeshConfig,
        work_item_id: str,
    ) -> list[str]:
        safe_work_item_id = work_item_id.strip()
        if not safe_work_item_id or "/" in safe_work_item_id or "\\" in safe_work_item_id:
            return []
        effective_workspace_root = self._effective_workspace_root(mesh_config)
        document_root = self._document_library_root(
            mesh_config,
            effective_workspace_root,
        )
        prompt_root = (
            document_root
            / "work-items"
            / safe_work_item_id
            / "debug"
            / "prompts"
        )
        artifacts: list[str] = []
        for root in [
            prompt_root,
            document_root
            / "work-items"
            / safe_work_item_id
            / "debug"
            / "safe-outputs",
        ]:
            if not root.exists() or not root.is_dir():
                continue
            for path in sorted(root.rglob("*")):
                if path.is_file() and path.suffix in {".txt", ".json"}:
                    try:
                        artifacts.append(path.relative_to(document_root).as_posix())
                    except ValueError:
                        continue
        return artifacts

    @staticmethod
    def artifact_renderer_url_template() -> str | None:
        return os.environ.get("AGENTIC_MESH_ARTIFACT_RENDERER_URL_TEMPLATE")

    @staticmethod
    def claim_lease_seconds() -> int:
        try:
            return int(os.environ.get("AGENTIC_MESH_CLAIM_LEASE_SECONDS", "21600"))
        except ValueError:
            return 21600

    def _effective_workspace_root(self, mesh_config: MeshConfig) -> Path:
        configured_root = Path(mesh_config.project.workspace.root)
        if configured_root.is_absolute():
            return configured_root.resolve()
        return (self.workspace_root / configured_root).resolve()

    @staticmethod
    def _document_library_root(
        mesh_config: MeshConfig,
        effective_workspace_root: Path,
    ) -> Path:
        configured_root = Path(mesh_config.project.document_library.root)
        if configured_root.is_absolute():
            return configured_root.resolve()
        return (effective_workspace_root / configured_root).resolve()

    def _journal_events(self, project_id: str) -> list[dict[str, Any]]:
        if self._journal_events_cache is not None:
            cached = self._journal_events_cache.get(project_id)
            if cached is not None:
                return cached
        path = self.state_root / "projects" / project_id / "journal" / "events.jsonl"
        if not path.exists():
            if self._journal_events_cache is not None:
                self._journal_events_cache[project_id] = []
            return []
        with path.open("r", encoding="utf-8") as handle:
            events = []
            for line in handle:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    events.append(event)
            if self._journal_events_cache is not None:
                self._journal_events_cache[project_id] = events
            return events

    def _queue_entries(self, project_id: str, work_item_id: str) -> list[dict[str, Any]]:
        queue_root = self.state_root / "projects" / project_id / "queues"
        if not queue_root.exists():
            return []
        entries: list[dict[str, Any]] = []
        for role_dir in sorted(path for path in queue_root.iterdir() if path.is_dir()):
            role_id = role_dir.name
            for queue_state in ["pending", "completed"]:
                for path in sorted((role_dir / queue_state).glob("*.json")):
                    try:
                        entry = self._queue_entry(path, role_id, queue_state, work_item_id)
                    except (OSError, json.JSONDecodeError):
                        entry = None
                    if entry:
                        entries.append(entry)
            for path in sorted((role_dir / "claimed").glob("*/*.json")):
                try:
                    entry = self._queue_entry(path, role_id, "claimed", work_item_id)
                except (OSError, json.JSONDecodeError):
                    entry = None
                if entry:
                    entries.append(entry)
        return entries

    def _queue_entry(
        self,
        path: Path,
        role_id: str,
        queue_state: str,
        work_item_id: str,
    ) -> dict[str, Any] | None:
        with path.open("r", encoding="utf-8") as handle:
            message = json.load(handle)
        payload = message.get("payload") or {}
        if payload.get("work_item_id") != work_item_id:
            return None
        return {
            "role_id": role_id,
            "queue_state": queue_state,
            "message_id": message.get("message_id"),
            "message_type": message.get("type"),
            "queue_item_id": payload.get("queue_item_id"),
            "title": payload.get("title"),
            "summary": payload.get("summary"),
            "source_anchor": payload.get("source_anchor"),
            "lifecycle_state": payload.get("lifecycle_state"),
            "created_at": message.get("created_at"),
            "claimed_at": message.get("claimed_at"),
            "claimed_by": message.get("claimed_by"),
            "claim_age_seconds": (
                telemetry.elapsed_seconds(message.get("claimed_at"))
                if queue_state == "claimed"
                else None
            ),
        }

    @staticmethod
    def _work_item_metadata(
        work_item_id: str,
        queue_entries: list[dict[str, Any]],
    ) -> tuple[str, str | None]:
        title_source = next(
            (entry.get("title") for entry in queue_entries if entry.get("title")),
            None,
        )
        summary_source = next(
            (entry.get("summary") for entry in queue_entries if entry.get("summary")),
            None,
        )
        title = status_dashboard.human_title(title_source or work_item_id)
        summary = status_dashboard.human_summary(summary_source or title_source)
        return title, summary


    @staticmethod
    def _notification_state(events: list[dict[str, Any]]) -> dict[str, Any]:
        failed = [
            event
            for event in events
            if event.get("event_type") == "problem_status_notification_failed"
        ]
        queued = [
            event
            for event in events
            if event.get("event_type") == "problem_status_notification_queued"
        ]
        if failed:
            latest = failed[-1]
            return {
                "status": "failed",
                "error_class": latest.get("notification_error_class"),
                "updated_at": latest.get("timestamp"),
            }
        if queued:
            return {
                "status": "queued",
                "updated_at": queued[-1].get("timestamp"),
            }
        return {"status": "not_queued"}

    @staticmethod
    def _current_work_item_state(
        events: list[dict[str, Any]],
        active_claims: list[dict[str, Any]],
        pending: list[dict[str, Any]],
        claim_lease_seconds: int,
    ) -> dict[str, Any]:
        if active_claims:
            claim = sorted(
                active_claims,
                key=lambda entry: str(entry.get("claimed_at") or ""),
            )[-1]
            claim_age_seconds = claim.get("claim_age_seconds")
            stale = (
                isinstance(claim_age_seconds, int | float)
                and claim_lease_seconds > 0
                and claim_age_seconds >= claim_lease_seconds
            )
            return {
                "status": "stale_claim" if stale else "running",
                "role_id": claim.get("role_id"),
                "role_instance_id": claim.get("claimed_by"),
                "lifecycle_state": claim.get("lifecycle_state"),
                "message_id": claim.get("message_id"),
                "since": claim.get("claimed_at"),
                "claim_age_seconds": claim_age_seconds,
                "stale": stale,
            }
        if pending:
            next_item = sorted(
                pending,
                key=lambda entry: str(entry.get("created_at") or ""),
            )[0]
            return {
                "status": "pending",
                "role_id": next_item.get("role_id"),
                "lifecycle_state": next_item.get("lifecycle_state"),
                "message_id": next_item.get("message_id"),
                "since": next_item.get("created_at"),
            }
        completed = [
            event for event in events if event.get("event_type") == "work_completed"
        ]
        if completed:
            latest = completed[-1]
            return {
                "status": str(latest.get("status") or "completed"),
                "role_id": latest.get("role_id"),
                "role_instance_id": latest.get("role_instance_id"),
                "lifecycle_state": latest.get("lifecycle_state"),
                "message_id": latest.get("message_id"),
                "since": latest.get("timestamp"),
                "reason_summary": latest.get("result_summary")
                or latest.get("reason"),
                "result_message": latest.get("result_message"),
            }
        if events:
            latest = events[-1]
            return {
                "status": "observed",
                "role_id": latest.get("role_id") or latest.get("target_role"),
                "lifecycle_state": latest.get("lifecycle_state")
                or latest.get("target_lifecycle_state"),
                "message_id": latest.get("message_id"),
                "since": latest.get("timestamp"),
            }
        return {"status": "not_found"}

    def _credential(
        self,
        mesh_config: MeshConfig,
        credential_id: str,
    ) -> AuthCredential:
        credential = mesh_config.project.auth_credentials.get(credential_id)
        if credential is None:
            raise KeyError(f"Unknown credential `{credential_id}`.")
        return credential


class ControllerAuthServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address,
        handler_class,
        service: ControllerAuthService,
    ):
        super().__init__(server_address, handler_class)
        self.service = service


class ControllerAuthHandler(BaseHTTPRequestHandler):
    server: ControllerAuthServer

    def do_GET(self) -> None:
        path = urlparse(self.path)
        if path.path == "/healthz":
            self._send_json(HTTPStatus.OK, {"status": "ok"})
            return
        if path.path == "/status":
            aggregate = self.server.service.status_dashboard_payload("status")
            self._send_html(HTTPStatus.OK, self._status_dashboard_page(aggregate))
            return
        if path.path == "/status.json":
            aggregate = self.server.service.status_dashboard_payload("status.json")
            self._send_json(HTTPStatus.OK, aggregate)
            return
        if path.path == "/agents/current":
            payload = self.server.service.current_agents_payload("agents.current")
            self._send_html(HTTPStatus.OK, self._current_agents_page(payload))
            return
        if path.path == "/agents/current.json":
            payload = self.server.service.current_agents_payload("agents.current.json")
            self._send_json(HTTPStatus.OK, payload)
            return
        if path.path == "/status/agents":
            self._redirect("/agents/current")
            return
        if path.path == "/status/agents.json":
            self._redirect("/agents/current.json")
            return
        if path.path == "/work-items":
            aggregate = self.server.service.status_dashboard_payload("work-items")
            self._send_html(
                HTTPStatus.OK,
                self._work_items_dashboard_page(
                    status_dashboard.work_items_payload(aggregate)
                ),
            )
            return
        if path.path == "/work-items.json":
            aggregate = self.server.service.status_dashboard_payload("work-items.json")
            self._send_json(
                HTTPStatus.OK,
                status_dashboard.work_items_payload(aggregate),
            )
            return
        if path.path == "/work-queue":
            aggregate = self.server.service.status_dashboard_payload("work-queue")
            self._send_html(
                HTTPStatus.OK,
                self._work_queue_dashboard_page(
                    status_dashboard.work_queue_payload(aggregate)
                ),
            )
            return
        if path.path == "/work-queue.json":
            aggregate = self.server.service.status_dashboard_payload("work-queue.json")
            self._send_json(
                HTTPStatus.OK,
                status_dashboard.work_queue_payload(aggregate),
            )
            return
        if path.path == "/queue":
            self._redirect("/work-queue")
            return
        if path.path == "/queue.json":
            aggregate = self.server.service.status_dashboard_payload("queue.json")
            self._send_json(
                HTTPStatus.OK,
                status_dashboard.work_queue_payload(aggregate),
            )
            return
        if path.path.startswith("/artifact-viewer/"):
            artifact_path = unquote(path.path.removeprefix("/artifact-viewer/"))
            file_path = self.server.service.resolve_artifact_path(artifact_path)
            if file_path is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            renderer_url = self._artifact_renderer_url(artifact_path)
            if renderer_url:
                self._redirect(renderer_url)
                return
            self._send_html(
                HTTPStatus.OK,
                self._artifact_viewer_page(artifact_path, file_path),
            )
            return
        if path.path.startswith("/artifacts/"):
            artifact_path = unquote(path.path.removeprefix("/artifacts/"))
            file_path = self.server.service.resolve_artifact_path(artifact_path)
            if file_path is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            self._send_file(file_path)
            return
        if path.path.startswith("/work-items/") and path.path.endswith(".json"):
            work_item_id = unquote(path.path.removeprefix("/work-items/")[:-5])
            payload = self.server.service.work_item_status(work_item_id)
            if payload["status"] == "not_found":
                self._send_json(HTTPStatus.NOT_FOUND, payload)
                return
            self._send_json(HTTPStatus.OK, payload)
            return
        if path.path.startswith("/work-items/"):
            work_item_id = unquote(path.path.removeprefix("/work-items/"))
            payload = self.server.service.work_item_status(work_item_id)
            if payload["status"] == "not_found":
                self._send_html(
                    HTTPStatus.NOT_FOUND,
                    self._layout("Work Item Not Found", "<p>Unknown work item.</p>"),
                )
                return
            self._send_html(HTTPStatus.OK, self._work_item_page(payload))
            return
        if path.path == "/auth/status.json":
            self._send_json(
                HTTPStatus.OK,
                {"credentials": self.server.service.credential_statuses()},
            )
            return
        if path.path in {"/", "/auth", "/auth/credentials"}:
            self._send_html(HTTPStatus.OK, self._credentials_page())
            return
        if path.path == "/auth/codex/session":
            session_id = parse_qs(path.query).get("id", [""])[0]
            session = self.server.service.session(session_id)
            if session is None:
                self._send_html(
                    HTTPStatus.NOT_FOUND,
                    self._layout("Session Not Found", "<p>Unknown session.</p>"),
                )
                return
            self._send_html(HTTPStatus.OK, self._session_page(session))
            return
        if path.path == "/auth/codex/session.json":
            session_id = parse_qs(path.query).get("id", [""])[0]
            payload = self.server.service.session_payload(session_id)
            if payload is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            self._send_json(HTTPStatus.OK, payload)
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:
        path = urlparse(self.path)
        try:
            form = self._read_form()
            if path.path == "/auth/secret":
                result = self.server.service.store_secret(
                    form.get("credential", ""),
                    form.get("secret_value", ""),
                    overwrite=form.get("overwrite") == "yes",
                )
                self._redirect(
                    "/auth/credentials?"
                    + urlencode(
                        {
                            "notice": (
                                f"Stored secret for {result['credential']}."
                            )
                        }
                    )
                )
                return
            if path.path == "/auth/codex/start":
                session = self.server.service.start_codex_oauth_login(
                    form.get("credential", "")
                )
                self._redirect(
                    "/auth/codex/session?" + urlencode({"id": session.session_id})
                )
                return
            if path.path == "/human-responses":
                result = self.server.service.record_human_response(form)
                status = (
                    HTTPStatus.OK
                    if result.get("accepted") or result.get("duplicate")
                    else HTTPStatus.BAD_REQUEST
                )
                self._send_json(status, result)
                return
            if path.path.startswith("/work-items/") and path.path.endswith("/actions"):
                work_item_id = unquote(
                    path.path.removeprefix("/work-items/").removesuffix("/actions")
                )
                result = self.server.service.execute_work_item_action(
                    work_item_id,
                    action=form.get("action", ""),
                    form=form,
                )
                self._redirect(
                    "/work-items/"
                    + quote(work_item_id, safe="")
                    + "?"
                    + urlencode({"notice": result["message"]})
                )
                return
        except Exception as exc:
            self._send_html(
                HTTPStatus.BAD_REQUEST,
                self._layout(
                    "Auth Action Failed",
                    f"<p>{html.escape(str(exc))}</p><p><a href=\"/auth/credentials\">Back</a></p>",
                ),
            )
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _credentials_page(self) -> str:
        path = urlparse(self.path)
        query = parse_qs(path.query)
        notice = query.get("notice", [""])[0]
        selected = query.get("credential", [""])[0]
        rows = []
        for credential in self.server.service.credential_statuses():
            roles = ", ".join(credential["roles"]) or "No roles"
            action = self._credential_action(credential)
            row_class = (
                ' class="selected"'
                if selected and selected == credential["credential"]
                else ""
            )
            rows.append(
                f"<tr id=\"credential-{html.escape(credential['credential'])}\"{row_class}>"
                f"<td><code>{html.escape(credential['credential'])}</code></td>"
                f"<td>{html.escape(credential['method'])}</td>"
                f"<td>{html.escape(credential['status'])}</td>"
                f"<td>{html.escape(credential['detail'])}</td>"
                f"<td>{html.escape(roles)}</td>"
                f"<td>{action}</td>"
                "</tr>"
            )
        notice_html = (
            f"<p class=\"notice\">{html.escape(notice)}</p>" if notice else ""
        )
        body = f"""
{notice_html}
<p>Credential values are never shown. Teams and Slack buttons should link here
for setup and status instead of carrying secrets in chat.</p>
<table>
  <thead>
    <tr>
      <th>Credential</th>
      <th>Method</th>
      <th>Status</th>
      <th>Detail</th>
      <th>Roles</th>
      <th>Action</th>
    </tr>
  </thead>
  <tbody>
    {''.join(rows)}
  </tbody>
</table>
"""
        return self._layout("Agentic Mesh Auth", body)

    def _credential_action(self, credential: dict[str, Any]) -> str:
        credential_id = html.escape(credential["credential"])
        method = credential["method"]
        if method == "codex_oauth_cache":
            if credential["status"] == "configured":
                return """
<button class="status-button" type="button" disabled title="OpenAI sign-in is configured">
  <svg aria-hidden="true" viewBox="0 0 24 24">
    <path d="M20 6 9 17l-5-5"></path>
  </svg>
  Signed in
</button>
"""
            return f"""
<form method="post" action="/auth/codex/start">
  <input type="hidden" name="credential" value="{credential_id}">
  <button type="submit">Sign in with OpenAI</button>
</form>
"""
        if credential["secret_ref"]:
            return f"""
<form method="post" action="/auth/secret">
  <input type="hidden" name="credential" value="{credential_id}">
  <input type="password" name="secret_value" autocomplete="off" placeholder="Secret value">
  <label><input type="checkbox" name="overwrite" value="yes"> Replace</label>
  <button type="submit">Store Secret</button>
</form>
"""
        return "No setup action"

    def _session_page(self, session: OAuthLoginSession) -> str:
        output = "\n".join(session.output) or "Waiting for Codex output..."
        openai_button = (
            f"""
<p id="openai-link-row">
  <a class="primary" id="openai-link" href="{html.escape(session.login_url)}" target="_blank" rel="noopener">
    Open OpenAI Sign-In
  </a>
</p>
"""
            if session.login_url
            else '<p id="openai-link-row">Preparing OpenAI sign-in...</p>'
        )
        code = (
            f"""
<div class="code-panel" id="code-panel">
  <p>OpenAI will ask for this one-time code:</p>
  <div class="code-row">
    <code class="login-code" id="login-code">{html.escape(session.user_code)}</code>
    <button class="icon-button" id="copy-code" type="button" title="Copy code" aria-label="Copy code">
      <svg aria-hidden="true" viewBox="0 0 24 24">
        <rect x="9" y="9" width="10" height="10" rx="2"></rect>
        <path d="M5 15V7a2 2 0 0 1 2-2h8"></path>
      </svg>
    </button>
    <span class="copy-status" id="copy-status" aria-live="polite"></span>
  </div>
</div>
<p id="waiting-code" hidden>Waiting for Codex to issue the one-time code...</p>
"""
            if session.user_code
            else """
<div class="code-panel" id="code-panel" hidden>
  <p>OpenAI will ask for this one-time code:</p>
  <div class="code-row">
    <code class="login-code" id="login-code"></code>
    <button class="icon-button" id="copy-code" type="button" title="Copy code" aria-label="Copy code">
      <svg aria-hidden="true" viewBox="0 0 24 24">
        <rect x="9" y="9" width="10" height="10" rx="2"></rect>
        <path d="M5 15V7a2 2 0 0 1 2-2h8"></path>
      </svg>
    </button>
    <span class="copy-status" id="copy-status" aria-live="polite"></span>
  </div>
</div>
<p id="waiting-code">Waiting for Codex to issue the one-time code...</p>
"""
        )
        instructions = (
            "OpenAI uses device-code sign-in here: open the sign-in page, "
            "enter the one-time code below, then return to this tab. Agentic Mesh "
            "will detect completion and store the credential cache automatically."
            if session.status == "running"
            else "This sign-in session has finished. Return to credentials to check status."
        )
        script = f"""
<script>
const sessionId = {json.dumps(session.session_id)};
const statusEl = document.getElementById("session-status");
const instructionsEl = document.getElementById("session-instructions");
const linkRow = document.getElementById("openai-link-row");
const codePanel = document.getElementById("code-panel");
const codeEl = document.getElementById("login-code");
const waitingCode = document.getElementById("waiting-code");
const outputEl = document.getElementById("technical-output");
const copyButton = document.getElementById("copy-code");
const copyStatus = document.getElementById("copy-status");

function runningInstructions() {{
  return "OpenAI uses device-code sign-in here: open the sign-in page, enter the one-time code below, then return to this tab. Agentic Mesh will detect completion and store the credential cache automatically.";
}}

function updateSession(data) {{
  statusEl.textContent = data.status;
  instructionsEl.textContent = data.status === "running"
    ? runningInstructions()
    : "This sign-in session has finished. Return to credentials to check status.";
  if (data.login_url) {{
    linkRow.innerHTML = '<a class="primary" id="openai-link" target="_blank" rel="noopener">Open OpenAI Sign-In</a>';
    linkRow.querySelector("a").href = data.login_url;
  }}
  if (data.user_code) {{
    codeEl.textContent = data.user_code;
    codePanel.hidden = false;
    waitingCode.hidden = true;
  }}
  outputEl.textContent = data.output || "Waiting for Codex output...";
  if (data.status === "running") {{
    window.setTimeout(pollSession, 3000);
  }}
}}

async function pollSession() {{
  try {{
    const response = await fetch("/auth/codex/session.json?id=" + encodeURIComponent(sessionId), {{
      cache: "no-store"
    }});
    if (response.ok) {{
      updateSession(await response.json());
    }} else {{
      window.setTimeout(pollSession, 5000);
    }}
  }} catch (_error) {{
    window.setTimeout(pollSession, 5000);
  }}
}}

function fallbackCopyText(text) {{
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.top = "-1000px";
  textarea.style.left = "-1000px";
  document.body.appendChild(textarea);
  textarea.focus();
  textarea.select();
  let copied = false;
  try {{
    copied = document.execCommand("copy");
  }} finally {{
    document.body.removeChild(textarea);
  }}
  return copied;
}}

copyButton.addEventListener("click", async () => {{
  const code = codeEl.textContent.trim();
  if (!code) {{
    return;
  }}
  try {{
    if (!navigator.clipboard || !window.isSecureContext) {{
      throw new Error("Clipboard API unavailable");
    }}
    await navigator.clipboard.writeText(code);
    copyStatus.textContent = "Copied";
  }} catch (_error) {{
    if (fallbackCopyText(code)) {{
      copyStatus.textContent = "Copied";
    }} else {{
      const range = document.createRange();
      range.selectNodeContents(codeEl);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      copyStatus.textContent = "Select and copy manually";
    }}
  }}
}});

if (statusEl.textContent === "running") {{
  window.setTimeout(pollSession, 3000);
}}
</script>
"""
        body = f"""
<p>Status: <strong id="session-status">{html.escape(session.status)}</strong></p>
<p>Credential: <code>{html.escape(session.credential_id)}</code></p>
<p id="session-instructions">{html.escape(instructions)}</p>
{openai_button}
{code}
<details>
  <summary>Technical output</summary>
  <pre id="technical-output">{html.escape(output)}</pre>
</details>
<p><a href="/auth/credentials">Back to credentials</a></p>
{script}
"""
        return self._layout("Codex OAuth Login", body)

    def _status_dashboard_page(self, payload: dict[str, Any]) -> str:
        counts = payload["counts"]
        links = payload["links"]
        nav = self._dashboard_nav(links)
        attention = payload["attention_needed"]
        active = payload["active"]
        queue_preview = payload["promoted"]["queue_items"] + payload["unknown"]["queue_items"]
        body = f"""
{nav}
<p class="summary">
  <strong>Project:</strong> {html.escape(str(payload["project"]["project_id"]))}
  <br><strong>Generated:</strong> {html.escape(str(payload["generated_at"]))}
  <br><strong>Refresh:</strong> manual browser refresh
  <br><strong>Read only:</strong> true
</p>
<h2>Status Counts</h2>
{self._status_counts_strip(counts)}
<h2>Attention Needed</h2>
{self._mixed_rows(attention["work_items"], attention["queue_items"], empty="No attention-needed work found.")}
<h2>Active Work</h2>
{self._mixed_rows(active["work_items"], active["queue_items"], empty="No active work found.")}
<h2>Work Queue Summary</h2>
{self._queue_rows(queue_preview[:10], empty="No promoted or incomplete queue items found.")}
"""
        return self._layout("Status Dashboard", body)

    def _work_items_dashboard_page(self, payload: dict[str, Any]) -> str:
        rows_by_group = self._rows_by_group(payload["items"])
        sections = []
        for group in status_dashboard.GROUPS:
            sections.append(
                f"<h2>{html.escape(group.replace('_', ' ').title())}</h2>"
                + self._work_item_rows(
                    rows_by_group[group],
                    empty="No work items found for this group.",
                )
            )
        body = f"""
{self._dashboard_nav(payload["links"])}
<p class="summary">
  <strong>Project:</strong> {html.escape(str(payload["project"]["project_id"]))}
  <br><strong>Generated:</strong> {html.escape(str(payload["generated_at"]))}
  <br><strong>Total work items:</strong> {payload["counts"]["total"]}
</p>
{''.join(sections) if payload["items"] else '<p>No work items found for this project.</p>'}
"""
        return self._layout("Work Items", body)

    def _work_queue_dashboard_page(self, payload: dict[str, Any]) -> str:
        rows_by_group = self._rows_by_group(payload["items"])
        sections = []
        for group in status_dashboard.GROUPS:
            sections.append(
                f"<h2>{html.escape(group.replace('_', ' ').title())}</h2>"
                + self._queue_rows(
                    rows_by_group[group],
                    empty="No work queue items found for this group.",
                )
            )
        body = f"""
{self._dashboard_nav(payload["links"])}
<p class="summary">
  <strong>Project:</strong> {html.escape(str(payload["project"]["project_id"]))}
  <br><strong>Generated:</strong> {html.escape(str(payload["generated_at"]))}
  <br><strong>Total queue items:</strong> {payload["counts"]["total"]}
  <br><strong>Alias:</strong> /queue opens this Work Queue view.
</p>
{''.join(sections) if payload["items"] else '<p>No work queue items found for this project.</p>'}
"""
        return self._layout("Work Queue", body)

    def _dashboard_nav(self, links: dict[str, str]) -> str:
        return f"""
<p>
  <a href="{html.escape(links["status_html"])}">Status</a>
  <a href="{html.escape(links["work_items_html"])}">Work items</a>
  <a href="{html.escape(links["work_queue_html"])}">Work queue</a>
  <a href="{html.escape(links["current_agents_html"])}">Current agents</a>
  <a href="{html.escape(links["status_json"])}">Status JSON</a>
  <a href="{html.escape(links["work_items_json"])}">Work items JSON</a>
  <a href="{html.escape(links["work_queue_json"])}">Work queue JSON</a>
  <a href="{html.escape(links["current_agents_json"])}">Current agents JSON</a>
</p>
"""

    @staticmethod
    def _status_counts_strip(counts: dict[str, Any]) -> str:
        items = [
            ("Attention", counts["attention_needed"]),
            ("Active", counts["active"]),
            ("Promoted", counts["promoted"]),
            ("Terminal", counts["terminal"]),
            ("Unknown", counts["unknown"]),
            ("Work items", counts["total_work_items"]),
            ("Queue", counts["total_queue_items"]),
        ]
        return (
            '<div class="counts-strip">'
            + "".join(
                '<div class="count-tile">'
                f'<span class="count-label">{html.escape(label)}</span>'
                f'<strong>{html.escape(str(value))}</strong>'
                "</div>"
                for label, value in items
            )
            + "</div>"
        )

    def _current_agents_page(self, payload: dict[str, Any]) -> str:
        links = payload["links"]
        counts = payload["counts"]
        nav = f"""
<p>
  <a href="{html.escape(links["status_html"])}">Status</a>
  <a href="{html.escape(links["current_agents_json"])}">Current agents JSON</a>
</p>
"""
        condition_rows = "".join(
            "<tr>"
            f"<td>{html.escape(str(condition))}</td>"
            f"<td>{html.escape(str(count))}</td>"
            "</tr>"
            for condition, count in sorted(counts["by_condition"].items())
        )
        attention_html = self._current_agent_attention_rows(payload["attention"])
        agent_html = self._current_agent_rows(payload["agents"])
        body = f"""
{nav}
<p class="summary">
  <strong>Project:</strong> {html.escape(str(payload["project"]["project_id"]))}
  <br><strong>Generated:</strong> {html.escape(str(payload["generated_at"]))}
  <br><strong>Schema:</strong> {html.escape(str(payload["schema_version"]))}
  <br><strong>Refresh:</strong> manual browser refresh
  <br><strong>Read only:</strong> true
</p>
<h2>Condition Counts</h2>
{self._dashboard_table(["Condition", "Count"], [condition_rows] if condition_rows else [])}
<h2>Attention</h2>
{attention_html}
<h2>Current Agents</h2>
{agent_html}
"""
        return self._layout("Current Agents", body)

    def _current_agent_attention_rows(self, rows: list[dict[str, Any]]) -> str:
        if not rows:
            return "<p>No agent attention needed.</p>"
        rendered = []
        for row in rows:
            status_url = row.get("status_url")
            work_item = row.get("current_work_item_id") or "None"
            work_item_html = (
                f"<a href=\"{html.escape(str(status_url))}\">{html.escape(str(work_item))}</a>"
                if status_url
                else html.escape(str(work_item))
            )
            rendered.append(
                "<tr>"
                f"<td><code>{html.escape(str(row['role_instance_id']))}</code></td>"
                f"<td>{html.escape(str(row['condition_label']))}<br><code>{html.escape(str(row['condition']))}</code></td>"
                f"<td>{work_item_html}</td>"
                f"<td>{html.escape(str(row.get('lifecycle_state') or 'Unknown'))}</td>"
                f"<td>{html.escape(str(row.get('elapsed_seconds') or row.get('claim_age_seconds') or 'n/a'))}</td>"
                f"<td>{html.escape(str(row.get('evidence_source') or 'unknown'))}</td>"
                f"<td>{html.escape(str(row.get('next_action') or 'Next action unknown'))}</td>"
                f"<td>{html.escape(str(row.get('action_owner') or 'Unknown owner'))}</td>"
                "</tr>"
            )
        return self._dashboard_table(
            [
                "Role instance",
                "Condition",
                "Work item",
                "Lifecycle",
                "Elapsed or age",
                "Evidence",
                "Next action",
                "Owner",
            ],
            rendered,
        )

    def _current_agent_rows(self, rows: list[dict[str, Any]]) -> str:
        if not rows:
            return "<p>No configured role-agent instances found.</p>"
        rendered = []
        for row in rows:
            status_url = row.get("status_url")
            json_url = row.get("work_item_json_url")
            work_item = row.get("current_work_item_id") or "None"
            work_item_html = (
                f"<a href=\"{html.escape(str(status_url))}\">{html.escape(str(work_item))}</a>"
                if status_url
                else html.escape(str(work_item))
            )
            json_html = (
                f"<a href=\"{html.escape(str(json_url))}\">JSON</a>" if json_url else "None"
            )
            rendered.append(
                "<tr>"
                f"<td><code>{html.escape(str(row['role_instance_id']))}</code><br>{html.escape(str(row['role_id']))}</td>"
                f"<td>{html.escape(str(row['condition_label']))}<br><code>{html.escape(str(row['condition']))}</code></td>"
                f"<td>{html.escape(str(row.get('condition_reason') or ''))}</td>"
                f"<td>{work_item_html}<br>{json_html}</td>"
                f"<td>{html.escape(str(row.get('lifecycle_state') or 'None'))}</td>"
                f"<td>{html.escape(str(row.get('service_label') or 'unknown'))}</td>"
                f"<td>{html.escape(str(row.get('evidence_source') or 'unknown'))}<br>{html.escape(str(row.get('evidence_observed_at') or 'not observed'))}</td>"
                f"<td>{html.escape(str(row.get('pending_queue_count') or 0))}</td>"
                f"<td>{html.escape(str(row.get('elapsed_seconds') if row.get('elapsed_seconds') is not None else 'n/a'))}</td>"
                f"<td>{html.escape(str(row.get('next_action') or 'Next action unknown'))}<br>{html.escape(str(row.get('action_owner') or 'Unknown owner'))}</td>"
                "</tr>"
            )
        return self._dashboard_table(
            [
                "Agent",
                "Condition",
                "Reason",
                "Work item",
                "Lifecycle",
                "Service label",
                "Evidence",
                "Pending",
                "Elapsed",
                "Action",
            ],
            rendered,
        )

    @staticmethod
    def _rows_by_group(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        grouped = {group: [] for group in status_dashboard.GROUPS}
        for row in rows:
            grouped.setdefault(str(row.get("status_group") or "unknown"), []).append(row)
        return grouped

    def _mixed_rows(
        self,
        work_items: list[dict[str, Any]],
        queue_items: list[dict[str, Any]],
        *,
        empty: str,
    ) -> str:
        if not work_items and not queue_items:
            return f"<p>{html.escape(empty)}</p>"
        rows = []
        for row in work_items:
            item = self._linked_title_cell(
                title=str(row.get("title") or row["work_item_id"]),
                href=str(row["status_url"]),
                fallback_id=str(row["work_item_id"]),
                kind="Work item",
            )
            status = self._status_and_state_cell(
                row,
                state=str(row.get("lifecycle_state") or "Unknown"),
            )
            owner = self._responsibility_cell(
                owner=str(row.get("owner_role") or "Unknown owner"),
                extra_label="Updated",
                extra_value=str(row.get("updated_at") or row.get("started_at") or "Unknown time"),
            )
            need = self._next_step_cell(row)
            rows.append(
                "<tr>"
                f"<td>{item}</td>"
                f"<td>{status}</td>"
                f"<td>{owner}</td>"
                f"<td>{need}</td>"
                "</tr>"
            )
        for row in queue_items:
            target = row.get("work_item_status_url") or "/work-queue"
            item = self._linked_title_cell(
                title=str(row.get("title") or row["queue_item_id"]),
                href=str(target),
                fallback_id=str(row["queue_item_id"]),
                kind="Queue item",
            )
            status = self._status_and_state_cell(
                row,
                state=str(row.get("recommended_work_item_type") or "Unknown type"),
            )
            owner = self._responsibility_cell(
                owner=str(row.get("owner_role") or "Unknown owner"),
                extra_label="Updated",
                extra_value=str(row.get("updated_at") or row.get("created_at") or "Unknown time"),
            )
            need = self._next_step_cell(row)
            rows.append(
                "<tr>"
                f"<td>{item}</td>"
                f"<td>{status}</td>"
                f"<td>{owner}</td>"
                f"<td>{need}</td>"
                "</tr>"
            )
        return self._dashboard_table(
            [
                "Item",
                "Status",
                "Owner / Updated",
                "Need / Next Step",
            ],
            rows,
        )

    def _work_item_rows(self, rows: list[dict[str, Any]], *, empty: str) -> str:
        if not rows:
            return f"<p>{html.escape(empty)}</p>"
        rendered = []
        for row in rows:
            item = self._linked_title_cell(
                title=str(row.get("title") or row["work_item_id"]),
                href=str(row["status_url"]),
                fallback_id=str(row["work_item_id"]),
                kind=str(row.get("work_item_type") or "Work item"),
            )
            status = self._status_and_state_cell(
                row,
                state=str(row.get("lifecycle_state") or "Unknown"),
            )
            owner = self._responsibility_cell(
                owner=str(row.get("owner_role") or "Unknown owner"),
                extra_label="Instance",
                extra_value=str(row.get("role_instance_id") or "Unknown"),
            )
            need = self._next_step_cell(row)
            evidence = self._artifact_summary_cell(row)
            rendered.append(
                "<tr>"
                f"<td>{item}</td>"
                f"<td>{status}</td>"
                f"<td>{owner}</td>"
                f"<td>{need}</td>"
                f"<td>{evidence}</td>"
                "</tr>"
            )
        return self._dashboard_table(
            [
                "Title",
                "Status / Lifecycle",
                "Owner / Instance",
                "Need / Next Step",
                "Evidence",
            ],
            rendered,
        )

    @staticmethod
    def _linked_title_cell(
        *,
        title: str,
        href: str,
        fallback_id: str,
        kind: str,
    ) -> str:
        return (
            '<div class="detail-stack">'
            f'<a class="item-title" href="{html.escape(href)}">{html.escape(title)}</a>'
            f'<div class="detail-line muted">{html.escape(kind)} &middot; <code>{html.escape(fallback_id)}</code></div>'
            "</div>"
        )

    def _status_and_state_cell(self, row: dict[str, Any], *, state: str) -> str:
        return (
            '<div class="detail-stack">'
            f"{self._status_label(row)}"
            f'<div class="detail-line"><span class="muted">State:</span> {html.escape(state)}</div>'
            "</div>"
        )

    @staticmethod
    def _responsibility_cell(
        *,
        owner: str,
        extra_label: str,
        extra_value: str,
    ) -> str:
        return (
            '<div class="detail-stack">'
            f'<div class="detail-line"><span class="muted">Owner:</span> {html.escape(owner)}</div>'
            f'<div class="detail-line"><span class="muted">{html.escape(extra_label)}:</span> {html.escape(extra_value)}</div>'
            "</div>"
        )

    @staticmethod
    def _next_step_cell(row: dict[str, Any]) -> str:
        reason = str(row.get("attention_reason") or "None")
        next_action = str(row.get("next_action") or "Unknown")
        updated = str(row.get("updated_at") or row.get("started_at") or row.get("created_at") or "Unknown time")
        return (
            '<div class="detail-stack">'
            f'<div class="detail-line"><span class="muted">Reason:</span> {html.escape(reason)}</div>'
            f'<div class="detail-line"><span class="muted">Next:</span> {html.escape(next_action)}</div>'
            f'<div class="detail-line muted">Updated: {html.escape(updated)}</div>'
            "</div>"
        )

    @staticmethod
    def _artifact_summary_cell(row: dict[str, Any]) -> str:
        links = row.get("artifact_links") or []
        visible_links = links[:4]
        link_html = " ".join(
            f'<a href="{html.escape(str(link["viewer_url"]))}" target="_blank" rel="noopener noreferrer">'
            f'{html.escape(str(link["label"]))}</a>'
            for link in visible_links
        )
        remaining = len(links) - len(visible_links)
        more = f'<span class="muted">+{remaining} more</span>' if remaining > 0 else ""
        return (
            '<div class="detail-stack evidence-links">'
            f'<div class="detail-line">{html.escape(str(row.get("artifact_count", 0)))} total, '
            f'{html.escape(str(row.get("missing_artifact_count", 0)))} missing</div>'
            f'<div class="detail-line">{link_html or "No artifacts recorded."} {more}</div>'
            f'<div class="detail-line"><a href="{html.escape(str(row["json_url"]))}" target="_blank" rel="noopener noreferrer">JSON</a></div>'
            "</div>"
        )

    def _queue_rows(self, rows: list[dict[str, Any]], *, empty: str) -> str:
        if not rows:
            return f"<p>{html.escape(empty)}</p>"
        rendered = []
        for row in rows:
            source = row.get("source_anchor_summary") or {}
            promoted_link = (
                f"<a href=\"{html.escape(str(row['work_item_status_url']))}\">{html.escape(str(row.get('promoted_work_item_id')))}</a>"
                if row.get("work_item_status_url")
                else html.escape(str(row.get("promoted_work_item_id") or "None"))
            )
            source_text = "; ".join(
                html.escape(str(source.get(key) or ""))
                for key in [
                    "connector_type",
                    "connector_id",
                    "source_scope",
                    "source_anchor_ref",
                    "display_label",
                    "received_at",
                ]
                if source.get(key)
            ) or "Source unknown"
            rendered.append(
                "<tr>"
                f"<td>{self._status_label(row)}</td>"
                f"<td><code>{html.escape(str(row['queue_item_id']))}</code></td>"
                f"<td>{html.escape(str(row.get('title') or row['queue_item_id']))}</td>"
                f"<td>{html.escape(str(row.get('owner_role') or 'Unknown owner'))}</td>"
                f"<td>{html.escape(str(row.get('recommended_work_item_type') or 'Unknown'))}</td>"
                f"<td>{source_text}</td>"
                f"<td>{promoted_link}</td>"
                f"<td>{html.escape(str(row.get('blocker_reason') or ''))}</td>"
                f"<td>{html.escape(str(row.get('notification_failure_reason') or ''))}</td>"
                f"<td>{html.escape(str(row.get('next_action') or 'Unknown'))}</td>"
                f"<td>{html.escape(str(row.get('updated_at') or row.get('created_at') or 'Unknown'))}</td>"
                "</tr>"
            )
        return self._dashboard_table(
            [
                "Status",
                "Queue item",
                "Title",
                "Owner",
                "Type",
                "Source",
                "Promoted work",
                "Blocker",
                "Notification",
                "Next action",
                "Updated",
            ],
            rendered,
        )

    @staticmethod
    def _status_label(row: dict[str, Any]) -> str:
        classes = "status-label"
        if row.get("status_group") == "attention_needed":
            classes += " warning"
        group = str(row.get("status_group") or "unknown").replace("_", " ")
        return (
            f"<span class=\"{classes}\">"
            f"{html.escape(str(row.get('display_label') or row.get('status') or 'Unknown'))}"
            f"</span>"
            f"<div class=\"detail-line muted\">{html.escape(group)}</div>"
        )

    @staticmethod
    def _dashboard_table(headers: list[str], rows: list[str]) -> str:
        header_html = "".join(f"<th>{html.escape(header)}</th>" for header in headers)
        return f"""
<div class="table-scroll">
<table class="dashboard-table">
  <thead><tr>{header_html}</tr></thead>
  <tbody>{''.join(rows)}</tbody>
</table>
</div>
"""

    def _work_item_page(self, payload: dict[str, Any]) -> str:
        current = payload["current"]
        problem = payload.get("problem_status") or {}
        route = payload.get("current_route") or {}
        problem_html = ""
        if problem:
            problem_html = f"""
<h2>Current Problem</h2>
<p class="warning-box">
  <strong>{html.escape(str(problem.get("status_label") or problem.get("status") or "Problem"))}</strong>
  - {html.escape(str(problem.get("problem_label") or problem.get("problem_kind") or "unknown"))}
  <br><strong>Affected role:</strong> {html.escape(str(problem.get("affected_role") or "unknown"))}
  <br><strong>Lifecycle state:</strong> {html.escape(str(problem.get("lifecycle_state") or "unknown"))}
  <br><strong>Reason:</strong> {html.escape(str(problem.get("reason_summary") or problem.get("reason") or ""))}
  <br><strong>Next action:</strong> {html.escape(str(problem.get("next_action") or ""))}
  <br><strong>Action owner:</strong> {html.escape(str(problem.get("action_owner") or "unknown"))}
  <br><strong>Retryability:</strong> {html.escape(str(problem.get("retryability_label") or problem.get("retryable") or "unknown"))}
  <br><strong>Occurred:</strong> {html.escape(str(problem.get("occurred_at") or "unknown"))}
</p>
"""
        activation = payload.get("activation_evidence") or {}
        activation_html = ""
        if activation:
            impact = ", ".join(activation.get("impact_category_labels") or activation.get("deployment_impact_labels") or [])
            paths = ", ".join(activation.get("activation_path_labels") or [])
            targets = ", ".join(activation.get("target_labels") or [])
            activation_html = f"""
<h2>Activation Evidence</h2>
<p class="info-box">
  <strong>{html.escape(str(activation.get("failure_class_label") or activation.get("activation_status_label") or "Activation"))}</strong>
  <br><strong>Impact:</strong> {html.escape(impact or "none")}
  <br><strong>Activation paths:</strong> {html.escape(paths or "unknown")}
  <br><strong>Targets:</strong> {html.escape(targets or "target_unknown")}
  <br><strong>Source:</strong> <code>{html.escape(str(activation.get("source_status") or "unknown"))}</code>
  <br><strong>Activation:</strong> <code>{html.escape(str(activation.get("activation_status") or "unknown"))}</code>
  <br><strong>Smoke:</strong> <code>{html.escape(str(activation.get("smoke_status") or "unknown"))}</code>
  <br><strong>Failure class:</strong> <code>{html.escape(str(activation.get("failure_class") or "none"))}</code>
  <br><strong>Action owner:</strong> {html.escape(str(activation.get("action_owner") or "none"))}
  <br><strong>Next action:</strong> {html.escape(str(activation.get("next_action") or "none"))}
  <br><strong>Notification:</strong> <code>{html.escape(str(activation.get("notification_state") or "not_required"))}</code>
  <br><strong>Updated:</strong> {html.escape(str(activation.get("updated_at") or activation.get("activation_updated_at") or "unknown"))}
</p>
"""
        route_html = ""
        if route:
            route_html = f"""
<h2>Current Route</h2>
<p class="info-box">
  <strong>{html.escape(str(route.get("route_status") or "route_requested"))}</strong>
  - {html.escape(str(route.get("route_kind") or "configured_route"))}
  <br><strong>Source:</strong> {html.escape(str(route.get("source_role") or "unknown"))}
  at <code>{html.escape(str(route.get("source_lifecycle_state") or "unknown"))}</code>
  <br><strong>Target:</strong> {html.escape(str(route.get("target_role") or "unknown"))}
  at <code>{html.escape(str(route.get("target_lifecycle_state") or "unknown"))}</code>
  <br><strong>Route id:</strong> <code>{html.escape(str(route.get("route_id") or "unknown"))}</code>
  <br><strong>Defect:</strong> <code>{html.escape(str(route.get("defect_id") or "none"))}</code>
  <br><strong>Required change:</strong> {html.escape(str(route.get("required_change") or ""))}
  <br><strong>Evidence required:</strong> {html.escape(str(route.get("evidence_required") or ""))}
</p>
"""
        worker_runs = payload.get("worker_runs") or {}
        worker_run_html = ""
        if worker_runs.get("current") or worker_runs.get("recent"):
            worker_run_rows = []
            displayed_runs = list(worker_runs.get("current") or [])
            displayed_runs.extend(list(worker_runs.get("recent") or [])[:10])
            seen_runs: set[str] = set()
            for run in displayed_runs:
                run_id = str(run.get("run_id") or "unknown")
                if run_id in seen_runs:
                    continue
                seen_runs.add(run_id)
                worker_run_rows.append(
                    "<tr>"
                    f"<td><code>{html.escape(run_id)}</code></td>"
                    f"<td>{html.escape(str(run.get('role_instance_id') or ''))}</td>"
                    f"<td>{html.escape(str(run.get('run_status') or ''))}</td>"
                    f"<td>{html.escape(str(run.get('run_condition') or ''))}</td>"
                    f"<td>{html.escape(str(run.get('failure_class') or ''))}</td>"
                    f"<td>{html.escape(str(run.get('last_progress_at') or ''))}</td>"
                    "</tr>"
                )
            worker_run_html = "<h2>Worker Run Evidence</h2>" + self._dashboard_table(
                [
                    "Run",
                    "Role instance",
                    "Status",
                    "Condition",
                    "Failure",
                    "Last progress",
                ],
                worker_run_rows,
            )
        queue_rows = []
        for entry in payload["queue_entries"]:
            claim_age = entry.get("claim_age_seconds")
            claim_age_text = (
                f"{int(float(claim_age))}s"
                if isinstance(claim_age, int | float)
                else ""
            )
            is_stale_claim = (
                entry.get("queue_state") == "claimed"
                and isinstance(claim_age, int | float)
                and payload.get("claim_lease_seconds", 0) > 0
                and float(claim_age) >= float(payload.get("claim_lease_seconds", 0))
            )
            row_class = ' class="stale-claim"' if is_stale_claim else ""
            stale_label = (
                ' <span class="warning">stale claim</span>' if is_stale_claim else ""
            )
            queue_rows.append(
                f"<tr{row_class}>"
                f"<td>{html.escape(str(entry.get('queue_state') or ''))}{stale_label}</td>"
                f"<td>{html.escape(str(entry.get('role_id') or ''))}</td>"
                f"<td>{html.escape(str(entry.get('lifecycle_state') or ''))}</td>"
                f"<td><code>{html.escape(str(entry.get('message_id') or ''))}</code></td>"
                f"<td>{html.escape(str(entry.get('claimed_by') or ''))}</td>"
                f"<td>{html.escape(str(entry.get('claimed_at') or entry.get('created_at') or ''))}"
                f"{' (' + html.escape(claim_age_text) + ')' if claim_age_text else ''}</td>"
                "</tr>"
            )
        timeline_rows = []
        for event in payload["timeline"][-80:]:
            role = event.get("role_id") or event.get("source_role") or ""
            target = event.get("target_role") or ""
            lifecycle = (
                event.get("lifecycle_state")
                or event.get("target_lifecycle_state")
                or event.get("source_lifecycle_state")
                or ""
            )
            detail = event.get("status") or event.get("message_type") or ""
            timeline_rows.append(
                "<tr>"
                f"<td>{html.escape(str(event.get('timestamp') or ''))}</td>"
                f"<td>{html.escape(str(event.get('event_type') or ''))}</td>"
                f"<td>{html.escape(str(role))}</td>"
                f"<td>{html.escape(str(target))}</td>"
                f"<td>{html.escape(str(lifecycle))}</td>"
                f"<td>{html.escape(str(detail))}</td>"
                "</tr>"
            )
        artifact_items_parts = []
        artifact_records = payload.get("artifact_records") or [
            {"path": path, "exists": True} for path in payload["artifacts"]
        ]
        for record in artifact_records:
            path = str(record.get("path") or "")
            label = str(record.get("label") or path)
            if record.get("exists"):
                artifact_items_parts.append(
                    "<li>"
                    f"<a href=\"/artifact-viewer/{quote(path, safe='')}\" target=\"_blank\" rel=\"noopener noreferrer\">"
                    f"{html.escape(label)} <code>{html.escape(path)}</code>"
                    "</a>"
                    f" <a class=\"source-link\" href=\"/artifacts/{quote(path, safe='')}\" target=\"_blank\" rel=\"noopener noreferrer\">source</a>"
                    "</li>"
                )
            else:
                artifact_items_parts.append(
                    "<li class=\"missing-artifact\">"
                    f"<code>{html.escape(path)}</code>"
                    ' <span class="warning">Missing artifact</span>'
                    "</li>"
                )
        artifact_items = "".join(artifact_items_parts) or "<li>None recorded</li>"
        teams_items = "".join(
            "<li>"
            f"{html.escape(str(item.get('timestamp') or ''))} "
            f"{html.escape(str(item.get('channel') or 'unknown'))}: "
            f"<code>{html.escape(str(item.get('teams_activity_id') or ''))}</code>"
            "</li>"
            for item in payload["teams_messages"]
        ) or "<li>None recorded</li>"
        json_path = (
            "/work-items/"
            + quote(str(payload["work_item_id"]), safe="")
            + ".json"
        )
        stale_notice = (
            "<p class=\"warning-box\">This work item has a stale claimed message. "
            "The owning role instance should reclaim it automatically on its next loop.</p>"
            if current.get("stale")
            else ""
        )
        title = str(payload.get("title") or payload["work_item_id"])
        summary_text = str(payload.get("summary") or "No summary captured yet.")
        work_item_label = "Work Item " + str(payload["work_item_id"])
        human_gate = payload.get("human_gate_summary") or {}
        current_gate = human_gate.get("current_human_gate") or {}
        next_gate = human_gate.get("next_human_gate") or {}
        unblock = payload.get("unblock_guidance") or {}
        recovery = payload.get("recovery_status") or {}
        query = parse_qs(urlparse(self.path).query)
        notice = query.get("notice", [""])[0]
        notice_html = (
            f"<p class=\"notice\">{html.escape(notice)}</p>" if notice else ""
        )
        unblock_actions = "".join(
            "<li>"
            f"<strong>{html.escape(str(action.get('label') or 'Action'))}</strong>"
            f"{self._work_item_action_form(payload, action)}"
            f"<pre>{html.escape(str(action.get('command') or ''))}</pre>"
            "</li>"
            for action in unblock.get("available_actions") or []
        )
        if not unblock_actions:
            unblock_actions = "<li>No direct action command is available for this state.</li>"
        manual_actions = self._manual_work_item_action_forms(payload, recovery)
        can_answer = "yes" if unblock.get("can_user_answer_now") else "no"
        unblock_html = f"""
<h2>How To Unblock</h2>
<div class="info-box">
  <strong>{html.escape(str(unblock.get("label") or "Unblock guidance unavailable"))}</strong>
  <br><strong>Can sponsor answer now:</strong> {html.escape(can_answer)}
  <br><strong>Action owner:</strong> {html.escape(str(unblock.get("action_owner") or "unknown"))}
  <br><strong>State:</strong> <code>{html.escape(str(unblock.get("state") or "unknown"))}</code>
  <p>{html.escape(str(unblock.get("summary") or "No unblock guidance captured."))}</p>
  <ul>{unblock_actions}</ul>
  {manual_actions}
</div>
"""
        human_gate_html = f"""
<h2>Human Gate Summary</h2>
<p class="info-box">
  <strong>{html.escape(str(human_gate.get("display_label") or "Unknown"))}</strong>
  <br><strong>Status:</strong> <code>{html.escape(str(human_gate.get("status") or "unknown"))}</code>
  <br><strong>Current gate:</strong> <code>{html.escape(str(current_gate.get("gate_id") or "none"))}</code>
  <br><strong>Next gate:</strong> <code>{html.escape(str(next_gate.get("gate_id") or "none"))}</code>
  <br><strong>Approval request:</strong> <code>{html.escape(str(human_gate.get("approval_request_id") or human_gate.get("response_request_id") or "none"))}</code>
  <br><strong>Notification attempt:</strong> <code>{html.escape(str(human_gate.get("notification_attempt_id") or "none"))}</code>
  <br><strong>Attention:</strong> {html.escape(str(human_gate.get("attention_reason") or "none"))}
</p>
"""
        body = f"""
<p class="summary">
  <strong>{html.escape(work_item_label)}</strong>
  <br><br>
  {html.escape(summary_text)}
  <br><br><strong>Status:</strong> {html.escape(str(payload["status"]))}
  <br><strong>Current role:</strong> {html.escape(str(current.get("role_id") or "none"))}
  <br><strong>Lifecycle state:</strong> {html.escape(str(current.get("lifecycle_state") or "none"))}
  <br><strong>Message:</strong> <code>{html.escape(str(current.get("message_id") or "none"))}</code>
  <br><strong>Since:</strong> {html.escape(str(current.get("since") or "unknown"))}
  <br><strong>Claim age:</strong> {html.escape(str(int(float(current.get("claim_age_seconds") or 0))) + "s" if current.get("claim_age_seconds") is not None else "n/a")}
</p>
{stale_notice}
{notice_html}
<p><a href="{html.escape(json_path)}">JSON status</a></p>
{unblock_html}
{human_gate_html}
{problem_html}
{activation_html}
{route_html}
{worker_run_html}
<h2>Queue</h2>
<table>
  <thead>
    <tr>
      <th>State</th>
      <th>Role</th>
      <th>Lifecycle</th>
      <th>Message</th>
      <th>Claimed By</th>
      <th>Timestamp</th>
    </tr>
  </thead>
  <tbody>{''.join(queue_rows)}</tbody>
</table>
<h2>Artifacts</h2>
<ul>{artifact_items}</ul>
<h2>Teams Messages</h2>
<ul>{teams_items}</ul>
<h2>Timeline</h2>
<table>
  <thead>
    <tr>
      <th>Time</th>
      <th>Event</th>
      <th>Role</th>
      <th>Target</th>
      <th>Lifecycle</th>
      <th>Detail</th>
    </tr>
  </thead>
  <tbody>{''.join(timeline_rows)}</tbody>
</table>
"""
        return self._layout(title, body)

    def _work_item_action_form(
        self,
        payload: dict[str, Any],
        action: dict[str, Any],
    ) -> str:
        action_name = str(action.get("action") or "")
        if action_name not in {"retry", "recover", "supersede"}:
            return ""
        recovery = payload.get("recovery_status") or {}
        current = payload.get("current") or {}
        work_item_id = str(payload["work_item_id"])
        fields = {
            "action": action_name,
            "expected_revision": str(recovery.get("revision") or 1),
            "lifecycle_state": str(
                recovery.get("lifecycle_state")
                or current.get("lifecycle_state")
                or ""
            ),
            "affected_role": str(
                recovery.get("affected_role") or current.get("role_id") or ""
            ),
            "work_item_type": str(recovery.get("work_item_type") or ""),
            "queue_item_id": str(recovery.get("queue_item_id") or ""),
            "actor": "operator" if action_name == "recover" else "sponsor",
            "reason": _default_work_item_action_reason(action_name),
        }
        replacement_html = ""
        if action_name == "supersede":
            replacement_html = (
                '<label>Replacement work item '
                '<input name="replacement_work_item_id" required '
                'placeholder="work-..."></label>'
            )
        button_label = {
            "retry": "Run Retry",
            "recover": "Mark Repaired And Recover",
            "supersede": "Supersede",
        }[action_name]
        return (
            f"<form class=\"inline-action\" method=\"post\" action=\"/work-items/{quote(work_item_id, safe='')}/actions\">"
            + "".join(
                f"<input type=\"hidden\" name=\"{html.escape(name)}\" value=\"{html.escape(value)}\">"
                for name, value in fields.items()
            )
            + replacement_html
            + f"<button type=\"submit\">{html.escape(button_label)}</button>"
            + "</form>"
        )

    def _manual_work_item_action_forms(
        self,
        payload: dict[str, Any],
        recovery: dict[str, Any],
    ) -> str:
        problem = payload.get("problem_status") or {}
        if not problem and not recovery:
            return ""
        current = payload.get("current") or {}
        work_item_id = str(payload["work_item_id"])
        base_fields = {
            "expected_revision": str(recovery.get("revision") or 1),
            "lifecycle_state": str(
                recovery.get("lifecycle_state")
                or current.get("lifecycle_state")
                or ""
            ),
            "affected_role": str(
                recovery.get("affected_role")
                or problem.get("affected_role")
                or current.get("role_id")
                or ""
            ),
            "actor": "operator",
        }

        def form(action: str, label: str, reason: str, danger: bool = False) -> str:
            fields = {
                **base_fields,
                "action": action,
                "reason": reason,
            }
            class_name = "danger-button" if danger else "secondary-button"
            return (
                f"<form class=\"inline-action\" method=\"post\" action=\"/work-items/{quote(work_item_id, safe='')}/actions\">"
                + "".join(
                    f"<input type=\"hidden\" name=\"{html.escape(name)}\" value=\"{html.escape(value)}\">"
                    for name, value in fields.items()
                )
                + f"<button class=\"{class_name}\" type=\"submit\">{html.escape(label)}</button>"
                + "</form>"
            )

        return (
            "<div class=\"manual-actions\">"
            "<h3>Manual Closure</h3>"
            "<p>Use these only when you have verified the item should no longer be worked by agents.</p>"
            + form(
                "complete",
                "Mark Complete",
                "Manual operator marked the work item complete.",
            )
            + form(
                "cancel",
                "Cancel",
                "Manual operator cancelled the work item.",
                danger=True,
            )
            + "</div>"
        )

    def _artifact_viewer_page(self, artifact_path: str, file_path: Path) -> str:
        raw_href = "/artifacts/" + quote(artifact_path, safe="")
        suffix = file_path.suffix.lower()
        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        title = f"Artifact {artifact_path}"
        if suffix in {".md", ".markdown"}:
            content = file_path.read_text(encoding="utf-8", errors="replace")
            markdown_json = json.dumps(content).replace("</", "<\\/")
            body = f"""
<p class="artifact-meta">
  <code>{html.escape(artifact_path)}</code>
  <a class="source-link" href="{html.escape(raw_href)}" target="_blank" rel="noopener noreferrer">source</a>
</p>
<article id="markdown-rendered" class="markdown-body"></article>
<script type="application/json" id="artifact-markdown">{markdown_json}</script>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<script type="module">
import mermaid from "https://cdn.jsdelivr.net/npm/mermaid/dist/mermaid.esm.min.mjs";

const sourceEl = document.getElementById("artifact-markdown");
const targetEl = document.getElementById("markdown-rendered");
const markdown = JSON.parse(sourceEl.textContent || '""');
mermaid.initialize({{ startOnLoad: false, securityLevel: "strict" }});
targetEl.innerHTML = marked.parse(markdown, {{ mangle: false, headerIds: true }});
sanitizeRenderedMarkdown(targetEl);
const diagrams = targetEl.querySelectorAll("pre code.language-mermaid, code.language-mermaid");
diagrams.forEach((node, index) => {{
  const container = document.createElement("div");
  container.className = "mermaid";
  container.textContent = node.textContent || "";
  container.id = `mermaid-artifact-${{index}}`;
  const pre = node.closest("pre");
  if (pre) {{
    pre.replaceWith(container);
  }} else {{
    node.replaceWith(container);
  }}
}});
await mermaid.run({{ querySelector: ".mermaid" }});

function sanitizeRenderedMarkdown(root) {{
  root.querySelectorAll("script, style, iframe, object, embed, link").forEach((node) => node.remove());
  root.querySelectorAll("*").forEach((node) => {{
    [...node.attributes].forEach((attribute) => {{
      const name = attribute.name.toLowerCase();
      const value = attribute.value.trim().toLowerCase();
      if (name.startsWith("on") || name === "style") {{
        node.removeAttribute(attribute.name);
      }}
      if ((name === "href" || name === "src") && !isSafeUrl(value)) {{
        node.removeAttribute(attribute.name);
      }}
    }});
  }});
}}

function isSafeUrl(value) {{
  return value === "" || value.startsWith("#") || value.startsWith("/") ||
    value.startsWith("./") || value.startsWith("../") ||
    value.startsWith("http://") || value.startsWith("https://") ||
    value.startsWith("mailto:");
}}
</script>
"""
        elif content_type.startswith("text/") or suffix in {".json", ".yaml", ".yml", ".toml", ".log"}:
            content = file_path.read_text(encoding="utf-8", errors="replace")
            body = f"""
<p class="artifact-meta">
  <code>{html.escape(artifact_path)}</code>
  <a class="source-link" href="{html.escape(raw_href)}" target="_blank" rel="noopener noreferrer">source</a>
</p>
<pre class="artifact-source">{html.escape(content)}</pre>
"""
        elif content_type.startswith("image/"):
            body = f"""
<p class="artifact-meta">
  <code>{html.escape(artifact_path)}</code>
  <a class="source-link" href="{html.escape(raw_href)}" target="_blank" rel="noopener noreferrer">source</a>
</p>
<img class="artifact-image" src="{html.escape(raw_href)}" alt="{html.escape(artifact_path)}">
"""
        elif content_type == "application/pdf":
            body = f"""
<p class="artifact-meta">
  <code>{html.escape(artifact_path)}</code>
  <a class="source-link" href="{html.escape(raw_href)}" target="_blank" rel="noopener noreferrer">source</a>
</p>
<iframe class="artifact-frame" src="{html.escape(raw_href)}" title="{html.escape(artifact_path)}"></iframe>
"""
        else:
            body = f"""
<p class="artifact-meta">
  <code>{html.escape(artifact_path)}</code>
</p>
<p>This artifact type is best opened by the configured browser renderer or downloaded as source.</p>
<p><a href="{html.escape(raw_href)}" target="_blank" rel="noopener noreferrer">Open source artifact</a></p>
"""
        return self._layout(title, body)

    def _artifact_renderer_url(self, artifact_path: str) -> str | None:
        template = self.server.service.artifact_renderer_url_template()
        if not template:
            return None
        artifact_url = self._absolute_url(
            "/artifacts/" + quote(artifact_path, safe="")
        )
        replacements = {
            "artifact_url": quote(artifact_url, safe=":/?#[]@!$&'()*+,;=%"),
            "artifact_path": quote(artifact_path, safe=""),
        }
        if "{artifact_url}" in template or "{artifact_path}" in template:
            return template.format(**replacements)
        separator = "&" if "?" in template else "?"
        return (
            template
            + separator
            + urlencode(
                {
                    "artifact_url": artifact_url,
                    "artifact_path": artifact_path,
                }
            )
        )

    def _absolute_url(self, path: str) -> str:
        host = self.headers.get("Host") or (
            f"{self.server.server_address[0]}:{self.server.server_address[1]}"
        )
        scheme = self.headers.get("X-Forwarded-Proto") or "http"
        return f"{scheme}://{host}{path}"

    def _layout(self, title: str, body: str) -> str:
        return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{html.escape(title)}</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 2rem; line-height: 1.4; }}
    table {{ border-collapse: collapse; width: 100%; }}
    .table-scroll {{ overflow-x: auto; }}
    th, td {{ border: 1px solid #d4d4d4; padding: 0.5rem; vertical-align: top; }}
    td {{ overflow-wrap: anywhere; }}
    th {{ background: #f4f4f4; text-align: left; }}
    .dashboard-table th, .dashboard-table td {{ padding: 0.4rem 0.55rem; }}
    .dashboard-table td {{ font-size: 0.94rem; }}
    .counts-strip {{ display: flex; flex-wrap: wrap; gap: 0.5rem; margin: 0.75rem 0 1rem; }}
    .count-tile {{ background: #f8fafc; border: 1px solid #d4d4d4; min-width: 7.5rem; padding: 0.45rem 0.65rem; }}
    .count-label {{ color: #475569; display: block; font-size: 0.82rem; }}
    .count-tile strong {{ display: block; font-size: 1.25rem; line-height: 1.1; }}
    .detail-stack {{ display: grid; gap: 0.16rem; line-height: 1.25; }}
    .detail-line {{ margin: 0; }}
    .muted {{ color: #64748b; font-size: 0.84rem; }}
    .item-title {{ font-weight: 650; }}
    .evidence-links a {{ display: inline-block; margin: 0 0.45rem 0.15rem 0; }}
    tr.selected {{ outline: 3px solid #6aa1ff; }}
    input[type=password] {{ min-width: 18rem; }}
    pre {{ background: #111; color: #eee; padding: 1rem; white-space: pre-wrap; }}
    h2 {{ margin-top: 2rem; }}
    .notice {{ background: #e9f7ef; border: 1px solid #9bd7ad; padding: 0.75rem; }}
    .summary {{ background: #f8fafc; border: 1px solid #cbd5e1; padding: 1rem; }}
    .warning {{ color: #92400e; font-weight: 600; }}
    .warning-box {{ background: #fffbeb; border: 1px solid #f59e0b; color: #78350f; padding: 0.75rem; }}
    tr.stale-claim td {{ background: #fffbeb; }}
    .missing-artifact {{ background: #fffbeb; border-left: 4px solid #f59e0b; padding: 0.5rem 0.75rem; }}
    .primary {{ display: inline-block; background: #111827; color: white; padding: 0.75rem 1rem; text-decoration: none; }}
    .code-panel {{ border: 2px solid #111827; display: inline-block; padding: 1rem 1.25rem; margin: 1rem 0; }}
    .code-row {{ align-items: center; display: flex; gap: 0.75rem; }}
    .login-code {{ display: block; font-size: 2rem; letter-spacing: 0.08em; }}
    .icon-button {{ align-items: center; background: #111827; border: 0; color: white; cursor: pointer; display: inline-flex; height: 2.75rem; justify-content: center; width: 2.75rem; }}
    .icon-button svg {{ fill: none; height: 1.25rem; stroke: currentColor; stroke-linecap: round; stroke-linejoin: round; stroke-width: 2; width: 1.25rem; }}
    .status-button {{ align-items: center; background: #e9f7ef; border: 1px solid #166534; color: #166534; display: inline-flex; gap: 0.35rem; padding: 0.45rem 0.7rem; }}
    .status-label {{ font-weight: 700; }}
    .status-button svg {{ fill: none; height: 1rem; stroke: currentColor; stroke-linecap: round; stroke-linejoin: round; stroke-width: 2.5; width: 1rem; }}
    .copy-status {{ color: #166534; min-width: 4rem; }}
    .inline-action {{ align-items: center; display: flex; flex-wrap: wrap; gap: 0.5rem; margin: 0.5rem 0; }}
    .inline-action input {{ padding: 0.45rem; }}
    .inline-action button {{ background: #111827; border: 1px solid #111827; color: white; cursor: pointer; padding: 0.5rem 0.75rem; }}
    .inline-action .secondary-button {{ background: #f8fafc; border-color: #64748b; color: #0f172a; }}
    .inline-action .danger-button {{ background: #991b1b; border-color: #991b1b; color: white; }}
    .manual-actions {{ border-top: 1px solid #cbd5e1; margin-top: 1rem; padding-top: 1rem; }}
    .source-link {{ color: #4b5563; font-size: 0.85rem; margin-left: 0.5rem; }}
    .artifact-meta {{ background: #f8fafc; border-left: 4px solid #2563eb; padding: 1rem; }}
    .artifact-source {{ overflow: auto; padding: 1rem; white-space: pre-wrap; }}
    .artifact-image {{ height: auto; max-width: 100%; }}
    .artifact-frame {{ border: 1px solid #d4d4d4; height: 80vh; width: 100%; }}
    .markdown-body {{ max-width: 72rem; }}
    .markdown-body table {{ display: block; max-width: 100%; overflow-x: auto; width: max-content; }}
    .markdown-body pre {{ overflow: auto; padding: 1rem; }}
  </style>
</head>
<body>
  <h1>{html.escape(title)}</h1>
  {body}
</body>
</html>"""

    def _read_form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length") or "0")
        body = self.rfile.read(length).decode("utf-8")
        parsed = parse_qs(body, keep_blank_values=True)
        return {key: values[-1] for key, values in parsed.items()}

    def _redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status: HTTPStatus, payload: str) -> None:
        body = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header(
            "Content-Security-Policy",
            (
                "default-src 'none'; "
                "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; "
                "connect-src 'self'; "
                "frame-src 'self'; "
                "object-src 'none'; "
                "base-uri 'none'"
            ),
        )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        body = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)


def serve_controller_auth(
    *,
    host: str,
    port: int,
    service: ControllerAuthService,
) -> None:
    server = ControllerAuthServer((host, port), ControllerAuthHandler, service)
    server.serve_forever()


def _safe_int(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _parse_human_response_value(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _normalize_human_response_value(value: Any, response_type: str | None) -> Any:
    if response_type != "approve_not_approve" or not isinstance(value, str):
        return value
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    return {
        "approve": "approved",
        "approved": "approved",
        "not_approve": "not_approved",
        "not_approved": "not_approved",
        "reject": "not_approved",
        "rejected": "not_approved",
    }.get(normalized, value)


def _default_work_item_action_reason(action: str) -> str:
    return {
        "retry": "Retry requested from the controller status page.",
        "recover": "Runtime repair confirmed from the controller status page.",
        "supersede": "Superseded from the controller status page.",
        "complete": "Marked complete from the controller status page.",
        "cancel": "Cancelled from the controller status page.",
    }.get(action, "Controller status page action.")


def _first_url(text: str) -> str | None:
    text = _strip_ansi(text)
    match = re.search(r"https?://[^\s)>\"]+", text)
    if not match:
        return None
    return match.group(0).rstrip(".,")


def _first_device_code(text: str) -> str | None:
    text = _strip_ansi(text)
    match = re.search(r"\b[A-Z0-9]{4,}(?:-[A-Z0-9]{4,})+\b", text)
    if not match:
        return None
    return match.group(0)


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)


def _artifact_label_record(
    path: str,
    verification: dict[str, Any] | None = None,
) -> dict[str, str]:
    if verification:
        return {
            "label": str(verification.get("label") or "Unverified partial artifact"),
            "verification": str(verification.get("verification") or "unverified_partial"),
        }
    if "/debug/prompts/" in path and path.endswith(".prompt.txt"):
        return {"label": "Debug prompt audit", "verification": "debug"}
    if "/debug/prompts/" in path and path.endswith(".metadata.json"):
        return {"label": "Debug prompt metadata", "verification": "debug"}
    if "/debug/safe-outputs/" in path and path.endswith(".safe-outputs.json"):
        return {"label": "Safe-output audit", "verification": "debug"}
    if path.endswith("/lifecycle-flow.md"):
        return {"label": "Lifecycle flow", "verification": "verified"}
    return {"label": "Verified artifact", "verification": "verified"}
