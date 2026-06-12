from __future__ import annotations

from dataclasses import dataclass
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


@dataclass(frozen=True)
class RoleRunReceipt:
    run_id: str
    status: str
    terminal_tool: str
    safe_output_count: int
    assignment_id: str | None = None


class RoleService:
    def __init__(
        self,
        *,
        db: V2Database,
        role_id: str,
        role_instance_id: str,
        worker: Worker,
        safe_outputs: SafeOutputService | None = None,
    ) -> None:
        self.db = db
        self.role_id = role_id
        self.role_instance_id = role_instance_id
        self.worker = worker
        self.safe_outputs = safe_outputs or SafeOutputService(db)

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
            calls = self.worker.run(assignment)
        except Exception:
            self.db.complete_run(run_id, status="failed", terminal_tool=None)
            raise
        terminal_calls: list[SafeOutputCall] = []
        try:
            for call in calls:
                if call.role_id != self.role_id:
                    raise ValueError("worker emitted safe-output for another role")
                self.safe_outputs.record(run_id=run_id, call=call)
                if call.terminal:
                    terminal_calls.append(call)
            if not calls:
                raise ValueError("role worker did not emit any safe-output calls")
            if not terminal_calls:
                raise ValueError("role worker did not emit a terminal safe-output call")
        except Exception:
            self.db.complete_run(run_id, status="failed", terminal_tool=None)
            raise
        terminal_tool = terminal_calls[-1].tool_name
        self.db.complete_run(
            run_id,
            status="completed",
            terminal_tool=terminal_tool,
        )
        return RoleRunReceipt(
            run_id=run_id,
            status="completed",
            terminal_tool=terminal_tool,
            safe_output_count=len(calls),
            assignment_id=assignment.assignment_id,
        )

    def claim_next_assignment(self) -> RoleAssignment | None:
        row = self.db.claim_role_assignment(
            role_id=self.role_id,
            role_instance_id=self.role_instance_id,
        )
        if row is None:
            return None
        return _assignment_from_row(row, role_instance_id=self.role_instance_id)

    def run_next_assignment(self) -> RoleRunReceipt | None:
        assignment = self.claim_next_assignment()
        if assignment is None:
            return None
        run_id = f"run-{uuid4().hex}"
        try:
            receipt = self.run_assignment(assignment, run_id=run_id)
        except Exception as exc:
            self.db.fail_role_assignment(
                str(assignment.assignment_id),
                role_instance_id=self.role_instance_id,
                run_id=run_id,
                reason=str(exc),
            )
            raise
        self.db.complete_role_assignment(
            str(assignment.assignment_id),
            role_instance_id=self.role_instance_id,
            run_id=receipt.run_id,
            terminal_tool=receipt.terminal_tool,
        )
        return receipt


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
