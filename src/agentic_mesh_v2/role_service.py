from __future__ import annotations

import sys
from dataclasses import dataclass
from dataclasses import replace
from typing import Any
from typing import Protocol
from uuid import uuid4

from agentic_mesh_v2.db import V2Database
from agentic_mesh_v2.safe_outputs import SafeOutputCall
from agentic_mesh_v2.safe_outputs import SafeOutputService


class Worker(Protocol):
    def run(self, assignment: "RoleAssignment") -> list[SafeOutputCall]:
        ...


@dataclass(frozen=True)
class RoleAssignment:
    role_id: str
    role_instance_id: str
    work_item_id: str | None
    title: str
    summary: str
    assignment_id: str | None = None
    assignment_type: str | None = None
    source_ref: str | None = None
    visibility_scope: str | None = None
    payload: dict[str, Any] | None = None
    conversation_context: tuple[str, ...] = ()
    memory_context: tuple[str, ...] = ()
    generated_prompt: str | None = None
    run_id: str | None = None
    safe_output_transport: dict[str, Any] | None = None


@dataclass(frozen=True)
class RoleRunReceipt:
    run_id: str
    status: str
    terminal_tool: str
    safe_output_count: int
    assignment_id: str | None = None


@dataclass(frozen=True)
class RoleDrainReceipt:
    status: str
    processed_count: int
    receipts: tuple[RoleRunReceipt, ...]


@dataclass(frozen=True)
class RoleServiceTickReceipt:
    status: str
    recovered_count: int
    processed_count: int
    receipts: tuple[RoleRunReceipt, ...]


class RoleService:
    def __init__(
        self,
        *,
        db: V2Database,
        role_id: str,
        role_instance_id: str,
        worker: Worker,
        safe_outputs: SafeOutputService | None = None,
        prompt_assembler: Any | None = None,
        assignment_lease_seconds: int = 300,
    ) -> None:
        self.db = db
        self.role_id = role_id
        self.role_instance_id = role_instance_id
        self.worker = worker
        self.safe_outputs = safe_outputs or SafeOutputService(db)
        self.prompt_assembler = prompt_assembler
        self.assignment_lease_seconds = assignment_lease_seconds
        self.db.update_role_instance_status(
            role_id=self.role_id,
            role_instance_id=self.role_instance_id,
            status="idle",
            detail="Role service initialized.",
        )

    def run_assignment(self, assignment: RoleAssignment, *, run_id: str | None = None) -> RoleRunReceipt:
        if assignment.role_id != self.role_id:
            raise ValueError("assignment role does not match role service")
        run_id = run_id or f"run-{uuid4().hex}"
        self.db.create_run(
            run_id=run_id,
            role_id=self.role_id,
            role_instance_id=self.role_instance_id,
            work_item_id=assignment.work_item_id,
        )
        try:
            worker_assignment = replace(
                assignment,
                run_id=run_id,
                safe_output_transport={
                    "transport": "cli",
                    "run_id": run_id,
                    "role_id": self.role_id,
                    "record_command": [
                        sys.executable,
                        "-m",
                        "agentic_mesh_v2.cli",
                        "--db",
                        str(self.db.path),
                        "record-safe-output",
                        "--run-id",
                        run_id,
                        "--role-id",
                        self.role_id,
                    ],
                },
            )
            if self.prompt_assembler is not None:
                prompt = self.prompt_assembler.render(worker_assignment)
                self.db.record_agent_prompt(
                    prompt_id=f"prompt-{uuid4().hex}",
                    run_id=run_id,
                    role_id=self.role_id,
                    role_instance_id=self.role_instance_id,
                    assignment_id=assignment.assignment_id,
                    prompt_text=prompt.prompt_text,
                    component_manifest=prompt.component_manifest,
                )
                worker_assignment = replace(worker_assignment, generated_prompt=prompt.prompt_text)
            calls = self.worker.run(worker_assignment)
        except Exception:
            self.db.complete_run(run_id, status="failed", terminal_tool=None)
            raise
        try:
            for call in calls:
                if call.role_id != self.role_id:
                    raise ValueError("worker emitted safe-output for another role")
                self.safe_outputs.record(run_id=run_id, call=call)
            recorded_calls = self.db.list_safe_output_calls_for_run(run_id)
            for call in recorded_calls:
                if call.get("role_id") != self.role_id:
                    raise ValueError("worker emitted safe-output for another role")
            terminal_calls = [call for call in recorded_calls if call.get("terminal")]
            if not recorded_calls:
                raise ValueError("role worker did not emit any safe-output calls")
            if not terminal_calls:
                raise ValueError("role worker did not emit a terminal safe-output call")
        except Exception:
            self.db.complete_run(run_id, status="failed", terminal_tool=None)
            raise
        terminal_tool = str(terminal_calls[-1]["tool_name"])
        self.db.complete_run(
            run_id,
            status="completed",
            terminal_tool=terminal_tool,
        )
        return RoleRunReceipt(
            run_id=run_id,
            status="completed",
            terminal_tool=terminal_tool,
            safe_output_count=len(recorded_calls),
            assignment_id=assignment.assignment_id,
        )

    def claim_next_assignment(self) -> RoleAssignment | None:
        row = self.db.claim_role_assignment(
            role_id=self.role_id,
            role_instance_id=self.role_instance_id,
            lease_seconds=self.assignment_lease_seconds,
        )
        if row is None:
            return None
        return _assignment_from_row(row, role_instance_id=self.role_instance_id)

    def run_next_assignment(self) -> RoleRunReceipt | None:
        assignment = self.claim_next_assignment()
        if assignment is None:
            self.db.update_role_instance_status(
                role_id=self.role_id,
                role_instance_id=self.role_instance_id,
                status="idle",
                detail="No queued assignment for role.",
            )
            return None
        run_id = f"run-{uuid4().hex}"
        self.db.update_role_instance_status(
            role_id=self.role_id,
            role_instance_id=self.role_instance_id,
            status="active",
            current_assignment_id=assignment.assignment_id,
            last_run_id=run_id,
            detail="Processing role assignment.",
        )
        try:
            receipt = self.run_assignment(assignment, run_id=run_id)
        except Exception as exc:
            self.db.fail_role_assignment(
                str(assignment.assignment_id),
                role_instance_id=self.role_instance_id,
                run_id=run_id,
                reason=str(exc),
            )
            self.db.update_role_instance_status(
                role_id=self.role_id,
                role_instance_id=self.role_instance_id,
                status="failed",
                current_assignment_id=assignment.assignment_id,
                last_run_id=run_id,
                detail=str(exc),
            )
            raise
        self.db.complete_role_assignment(
            str(assignment.assignment_id),
            role_instance_id=self.role_instance_id,
            run_id=receipt.run_id,
            terminal_tool=receipt.terminal_tool,
            status=_assignment_status_for_terminal_tool(receipt.terminal_tool),
        )
        self.db.update_role_instance_status(
            role_id=self.role_id,
            role_instance_id=self.role_instance_id,
            status="idle",
            current_assignment_id=None,
            last_run_id=receipt.run_id,
            detail="Assignment completed.",
        )
        return receipt

    def refresh_assignment_lease(self, assignment_id: str) -> bool:
        return self.db.refresh_role_assignment_lease(
            assignment_id=assignment_id,
            role_instance_id=self.role_instance_id,
            lease_seconds=self.assignment_lease_seconds,
        )

    def recover_stale_assignments(self, *, limit: int = 50, reason: str | None = None) -> list[dict[str, Any]]:
        reason = reason or f"Recovered by {self.role_instance_id} role service maintenance tick."
        self.db.update_role_instance_status(
            role_id=self.role_id,
            role_instance_id=self.role_instance_id,
            status="recovering",
            detail="Recovering stale claimed assignments for role.",
        )
        recovered = self.db.recover_stale_role_assignments(
            role_id=self.role_id,
            limit=limit,
            reason=reason,
        )
        self.db.update_role_instance_status(
            role_id=self.role_id,
            role_instance_id=self.role_instance_id,
            status="idle",
            processed_count=0,
            detail=f"Recovered {len(recovered)} stale assignments.",
        )
        return recovered

    def drain_available_assignments(self, *, max_assignments: int = 10) -> RoleDrainReceipt:
        if max_assignments < 1:
            raise ValueError("max_assignments must be at least 1")
        receipts: list[RoleRunReceipt] = []
        while len(receipts) < max_assignments:
            receipt = self.run_next_assignment()
            if receipt is None:
                break
            receipts.append(receipt)
        self.db.update_role_instance_status(
            role_id=self.role_id,
            role_instance_id=self.role_instance_id,
            status="idle",
            processed_count=len(receipts),
            detail="Role service drained available assignments.",
        )
        return RoleDrainReceipt(
            status="idle",
            processed_count=len(receipts),
            receipts=tuple(receipts),
        )

    def run_service_tick(self, *, max_recoveries: int = 50, max_assignments: int = 10) -> RoleServiceTickReceipt:
        if max_recoveries < 1:
            raise ValueError("max_recoveries must be at least 1")
        if max_assignments < 1:
            raise ValueError("max_assignments must be at least 1")
        recovered = self.recover_stale_assignments(limit=max_recoveries)
        drain = self.drain_available_assignments(max_assignments=max_assignments)
        self.db.update_role_instance_status(
            role_id=self.role_id,
            role_instance_id=self.role_instance_id,
            status="idle",
            processed_count=drain.processed_count,
            detail=(
                f"Role service tick recovered {len(recovered)} stale assignments "
                f"and processed {drain.processed_count} assignments."
            ),
        )
        return RoleServiceTickReceipt(
            status="idle",
            recovered_count=len(recovered),
            processed_count=drain.processed_count,
            receipts=drain.receipts,
        )


def _assignment_from_row(row: dict[str, Any], *, role_instance_id: str) -> RoleAssignment:
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    context = payload.get("context")
    conversation_context = tuple(str(item) for item in context) if isinstance(context, list) else ()
    return RoleAssignment(
        role_id=str(row["role_id"]),
        role_instance_id=role_instance_id,
        work_item_id=row.get("work_item_id"),
        title=str(row["title"]),
        summary=str(row["summary"]),
        assignment_id=str(row["assignment_id"]),
        assignment_type=str(row["assignment_type"]),
        source_ref=str(row["source_ref"]),
        visibility_scope=str(row["visibility_scope"]),
        payload=payload,
        conversation_context=conversation_context,
    )


def _assignment_status_for_terminal_tool(terminal_tool: str) -> str:
    if terminal_tool in {"sponsor.ask_question", "human_response.request", "release.request_approval"}:
        return "waiting_human"
    if terminal_tool == "report.blocked":
        return "blocked"
    if terminal_tool == "report.incomplete":
        return "incomplete"
    return "completed"
