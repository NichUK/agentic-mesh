from __future__ import annotations

from agentic_mesh.models import (
    AgentRunResult,
    DocumentUpdate,
    FlowState,
    Handoff,
    Message,
    RoleInstanceConfig,
)


class WorkerAdapter:
    def run(
        self,
        instance: RoleInstanceConfig,
        message: Message,
        flow_state: FlowState,
    ) -> AgentRunResult:
        raise NotImplementedError


class StubCodexWorkerAdapter(WorkerAdapter):
    """Deterministic stand-in for the future codex-cli adapter."""

    def run(
        self,
        instance: RoleInstanceConfig,
        message: Message,
        flow_state: FlowState,
    ) -> AgentRunResult:
        title = message.payload.get("title", "Untitled work item")
        summary = message.payload.get("summary", message.payload.get("text", ""))
        work_item_id = message.payload.get("work_item_id", message.message_id)
        work_item_type = message.payload.get("work_item_type", "slice")

        content = (
            f"\n## {title}\n\n"
            f"- Work item: `{work_item_id}`\n"
            f"- Work item type: `{work_item_type}`\n"
            f"- Lifecycle state: `{flow_state.state_id}`\n"
            f"- State purpose: {flow_state.purpose}\n"
            f"- Owner role: `{instance.role_id}`\n"
            f"- Claimed by: `{instance.instance_id}`\n"
            f"- Source message: `{message.message_id}`\n"
            f"- Correlation id: `{message.correlation_id}`\n"
            f"- Summary: {summary}\n"
        )
        handoffs: list[Handoff] = []
        transition = flow_state.handoffs.get("completed")
        if transition:
            handoffs.append(
                Handoff(
                    target_role=transition.target_role,
                    message_type=transition.message_type,
                    payload={
                        "title": title,
                        "summary": summary,
                        "work_item_id": work_item_id,
                        "work_item_type": work_item_type,
                        "previous_lifecycle_state": flow_state.state_id,
                        "lifecycle_state": transition.target_state,
                        "source_message_id": message.message_id,
                    },
                )
            )

        return AgentRunResult(
            status="completed",
            message=f"Completed {flow_state.state_id} for {work_item_id}",
            document_updates=[
                DocumentUpdate(path=flow_state.artifact_path, content=content),
            ],
            handoffs=handoffs,
        )
