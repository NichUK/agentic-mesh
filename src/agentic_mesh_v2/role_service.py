from __future__ import annotations

from dataclasses import dataclass
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
    conversation_context: tuple[str, ...] = ()
    memory_context: tuple[str, ...] = ()


@dataclass(frozen=True)
class RoleRunReceipt:
    run_id: str
    status: str
    terminal_tool: str
    safe_output_count: int


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

    def run_assignment(self, assignment: RoleAssignment) -> RoleRunReceipt:
        if assignment.role_id != self.role_id:
            raise ValueError("assignment role does not match role service")
        run_id = f"run-{uuid4().hex}"
        self.db.create_run(
            run_id=run_id,
            role_id=self.role_id,
            role_instance_id=self.role_instance_id,
            work_item_id=assignment.work_item_id,
        )
        calls = self.worker.run(assignment)
        terminal_calls: list[SafeOutputCall] = []
        for call in calls:
            if call.role_id != self.role_id:
                raise ValueError("worker emitted safe-output for another role")
            self.safe_outputs.record(run_id=run_id, call=call)
            if call.terminal:
                terminal_calls.append(call)
        if not calls:
            self.db.complete_run(run_id, status="failed", terminal_tool=None)
            raise ValueError("role worker did not emit any safe-output calls")
        if not terminal_calls:
            self.db.complete_run(run_id, status="failed", terminal_tool=None)
            raise ValueError("role worker did not emit a terminal safe-output call")
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
        )
