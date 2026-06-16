from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys

from agentic_mesh_v3.agent import AgentMessage
from agentic_mesh_v3.agent import DatabaseAgentRunRecorder
from agentic_mesh_v3.agent import DatabaseAgentStatusReporter
from agentic_mesh_v3.agent import DatabaseConversationContext
from agentic_mesh_v3.agent import DatabaseTerminalToolCallAudit
from agentic_mesh_v3.agent import DatabaseWorkItemGovernanceContextProvider
from agentic_mesh_v3.agent import RoleAgentService
from agentic_mesh_v3.agent import RoleInstanceConfig
from agentic_mesh_v3.broker import BrokerAdapter
from agentic_mesh_v3.broker import InMemoryBrokerAdapter
from agentic_mesh_v3.connectors import LocalTeamsBridge
from agentic_mesh_v3.connectors import StakeholderBridge
from agentic_mesh_v3.connectors import StakeholderMessage
from agentic_mesh_v3.db import V3Database
from agentic_mesh_v3.deployment import CommandDeploymentTarget
from agentic_mesh_v3.deployment import DeploymentTarget
from agentic_mesh_v3.documents import DocumentLibraryAdapter
from agentic_mesh_v3.dogfood import DogfoodSponsorContact
from agentic_mesh_v3.memory import DatabaseRoleMemory
from agentic_mesh_v3.teams_ingress import DatabaseApprovalResponseRecorder
from agentic_mesh_v3.tools import V3ToolService


WORK_ITEM_ID = "work-v3-agent-service-e2e"
QUEUE_ITEM_ID = "queue-v3-agent-service-e2e"
RELEASE_ID = "release-v3-agent-service-e2e"
PRODUCT_APPROVAL_ID = "approval-v3-agent-service-product"


def run_agent_service_e2e_dogfood_slice(
    *,
    db: V3Database,
    document_library: DocumentLibraryAdapter,
    runtime_state_dir: Path,
    project_id: str = "agentic-mesh-dev",
    deployment_targets: dict[str, DeploymentTarget] | None = None,
    deployment_target_id: str = "local-smoke",
    broker: BrokerAdapter | None = None,
    broker_stream: str = "agent-inbox",
    stakeholder_bridge: StakeholderBridge | None = None,
    sponsor_contact: DogfoodSponsorContact | None = None,
) -> str:
    """Prove V3 progression through role services rather than a central script."""

    role_ids = (
        "engineering",
        "product-manager",
        "project-manager",
        "qa-engineer",
        "release-manager",
        "solution-architect",
    )
    broker = broker or InMemoryBrokerAdapter()
    broker.ensure_stream(
        broker_stream,
        [
            "project.context",
            *(f"agent.{role_id}" for role_id in role_ids),
            *(f"agent.{role_id}.relevance" for role_id in role_ids),
        ],
    )
    teams = stakeholder_bridge or LocalTeamsBridge(broker, stream=broker_stream, role_ids=role_ids)
    configured_targets = deployment_targets or {
        deployment_target_id: CommandDeploymentTarget(
            target_id=deployment_target_id,
            command=(sys.executable, "-c", "print('v3 agent-service smoke deployed')"),
            rollback_plan="Re-run the previous known-good local command target.",
        )
    }
    if deployment_target_id not in configured_targets:
        raise ValueError(f"deployment target is not configured: {deployment_target_id}")
    tools = V3ToolService(
        db,
        document_library=document_library,
        deployment_targets=configured_targets,
        stakeholder_bridge=teams,
        broker=broker,
        broker_stream=broker_stream,
    )

    teams.route_inbound(
        StakeholderMessage(
            connector="teams",
            message_id="msg-v3-agent-service-e2e",
            source_type="dm",
            sender_ref="sponsor",
            conversation_ref="dm:product-manager",
            text="Please run the V3 agent-service dogfood release proof.",
            reply_target_ref=sponsor_contact.target_ref if sponsor_contact is not None else None,
            reply_thread_ref=sponsor_contact.thread_ref if sponsor_contact is not None else None,
        )
    )

    _run_role(
        db=db,
        broker=broker,
        stream=broker_stream,
        runtime_state_dir=runtime_state_dir,
        project_id=project_id,
        role_id="product-manager",
        worker=_ProductManagerWorker(tools, project_id=project_id, sponsor_contact=sponsor_contact),
    )
    _record_product_approval(
        db=db,
        broker=broker,
        stream=broker_stream,
        sponsor_contact=sponsor_contact,
    )
    _run_role(
        db=db,
        broker=broker,
        stream=broker_stream,
        runtime_state_dir=runtime_state_dir,
        project_id=project_id,
        role_id="product-manager",
        worker=_ProductManagerWorker(tools, project_id=project_id, sponsor_contact=sponsor_contact),
    )
    _run_role(
        db=db,
        broker=broker,
        stream=broker_stream,
        runtime_state_dir=runtime_state_dir,
        project_id=project_id,
        role_id="engineering",
        worker=_EngineeringWorker(tools, project_id=project_id),
    )
    _run_role(
        db=db,
        broker=broker,
        stream=broker_stream,
        runtime_state_dir=runtime_state_dir,
        project_id=project_id,
        role_id="solution-architect",
        worker=_SolutionArchitectWorker(tools, project_id=project_id),
    )
    _run_role(
        db=db,
        broker=broker,
        stream=broker_stream,
        runtime_state_dir=runtime_state_dir,
        project_id=project_id,
        role_id="qa-engineer",
        worker=_QaWorker(tools, project_id=project_id),
    )
    _run_role(
        db=db,
        broker=broker,
        stream=broker_stream,
        runtime_state_dir=runtime_state_dir,
        project_id=project_id,
        role_id="release-manager",
        worker=_ReleaseManagerWorker(tools, project_id=project_id, deployment_target_id=deployment_target_id),
    )
    _run_role(
        db=db,
        broker=broker,
        stream=broker_stream,
        runtime_state_dir=runtime_state_dir,
        project_id=project_id,
        role_id="project-manager",
        worker=_ProjectManagerWorker(tools, project_id=project_id, sponsor_contact=sponsor_contact),
    )
    return WORK_ITEM_ID


@dataclass
class _ProductManagerWorker:
    tools: V3ToolService
    project_id: str
    sponsor_contact: DogfoodSponsorContact | None

    @property
    def role_instance_id(self) -> str:
        return f"{self.project_id}.product-manager.1"

    def run(self, prompt: str, message: AgentMessage) -> list[str]:
        del prompt
        if message.payload.get("message_type") == "stakeholder.message":
            return self._start_product_shaping(message)
        if message.payload.get("message_type") == "approval.response_recorded":
            return self._promote_after_approval(message)
        return _complete_noop(self.tools, self.role_instance_id, "Product Manager had no relevant action.")

    def _start_product_shaping(self, message: AgentMessage) -> list[str]:
        calls = []
        calls.append(
            self.tools.call(
                role_instance_id=self.role_instance_id,
                tool_name="backlog.upsert",
                payload={
                    "queue_item_id": QUEUE_ITEM_ID,
                    "title": "V3 agent-service dogfood release",
                    "summary": (
                        "Tiny V3 slice proving Teams intake, role-service inbox processing, "
                        "handoffs, QA, deployment, release, closure, and document indexes."
                    ),
                    "status": "promoted",
                    "owner_role": "product-manager",
                    "linked_work_item_id": WORK_ITEM_ID,
                    "source_ref": f"teams:{message.payload.get('source_message_id')}",
                },
            ).call_id
        )
        calls.append(
            self.tools.call(
                role_instance_id=self.role_instance_id,
                tool_name="work_item.upsert",
                payload={
                    "work_item_id": WORK_ITEM_ID,
                    "queue_item_id": QUEUE_ITEM_ID,
                    "title": "V3 agent-service dogfood release",
                    "description": "Prove one V3 end-to-end slice through real role-service inbox processing.",
                    "state": "shaping",
                    "owner_role": "product-manager",
                    "current_phase": "requirements",
                    "next_action": "Sponsor product sign-off.",
                    "governance": {
                        "phase": "requirements",
                        "accountable_role": "project-manager",
                        "responsible_roles": ["product-manager"],
                        "consulted_roles": ["sponsor"],
                        "informed_roles": ["delivery-manager"],
                        "sponsor_decision_points": ["product-signoff"],
                        "required_evidence": [f"work-items/{WORK_ITEM_ID}/index.md"],
                    },
                },
            ).call_id
        )
        approval_payload = {
            "approval_id": PRODUCT_APPROVAL_ID,
            "work_item_id": WORK_ITEM_ID,
            "question": "Approve the V3 agent-service dogfood release proof scope?",
            "current_phase": "requirements",
            "next_action": "Sponsor approval is required before implementation starts.",
        }
        if self.sponsor_contact is not None:
            approval_payload.update(
                {
                    "connector": self.sponsor_contact.connector,
                    "target_ref": self.sponsor_contact.target_ref,
                    "thread_ref": self.sponsor_contact.thread_ref,
                    "text_markdown": (
                        "**Product sign-off requested: V3 agent-service dogfood release**\n\n"
                        f"Please approve `{WORK_ITEM_ID}` so the role-service proof can continue.\n\n"
                        f"Respond with: `{PRODUCT_APPROVAL_ID} approved`"
                    ),
                }
            )
        calls.append(
            self.tools.call(
                role_instance_id=self.role_instance_id,
                tool_name="approval.request",
                payload=approval_payload,
            ).call_id
        )
        calls.append(_write_index(self.tools, self.role_instance_id, "shaping", "product-manager", "Product sign-off requested."))
        calls.append(_status_reply(self.tools, self.role_instance_id, message, "Product shaping started and sponsor sign-off requested."))
        return calls

    def _promote_after_approval(self, message: AgentMessage) -> list[str]:
        calls = []
        calls.append(
            self.tools.call(
                role_instance_id=self.role_instance_id,
                tool_name="decision.record",
                payload={
                    "work_item_id": WORK_ITEM_ID,
                    "summary": "Sponsor approved V3 agent-service dogfood release proof scope.",
                    "target_ref": PRODUCT_APPROVAL_ID,
                },
            ).call_id
        )
        calls.append(
            self.tools.call(
                role_instance_id=self.role_instance_id,
                tool_name="handoff.require",
                payload=_handoff_payload(
                    target_role="engineering",
                    phase="development",
                    accountable_role="engineering",
                    required_next_action="Implement the signed-off local dogfood proof and hand to QA.",
                    consulted_roles=["solution-architect"],
                    informed_roles=["project-manager", "delivery-manager"],
                ),
            ).call_id
        )
        calls.append(_write_index(self.tools, self.role_instance_id, "waiting_agent", "engineering", "Product approved and handed to Engineering."))
        calls.append(_status_reply(self.tools, self.role_instance_id, message, "Product approval recorded and the work was handed to Engineering."))
        return calls


@dataclass
class _EngineeringWorker:
    tools: V3ToolService
    project_id: str

    @property
    def role_instance_id(self) -> str:
        return f"{self.project_id}.engineering.1"

    def run(self, prompt: str, message: AgentMessage) -> list[str]:
        del prompt, message
        calls = [
            self.tools.call(
                role_instance_id=self.role_instance_id,
                tool_name="work_item.update_state",
                payload={
                    "work_item_id": WORK_ITEM_ID,
                    "state": "active",
                    "owner_role": "engineering",
                    "current_phase": "development",
                    "next_action": "Engineering evidence is ready; QA review required.",
                },
            ).call_id,
            self.tools.call(
                role_instance_id=self.role_instance_id,
                tool_name="consult.request",
                payload={
                    "work_item_id": WORK_ITEM_ID,
                    "target_role": "solution-architect",
                    "question": "Confirm this dogfood proof remains within the V3 agent-owned architecture.",
                },
            ).call_id,
            self.tools.call(
                role_instance_id=self.role_instance_id,
                tool_name="risk.register",
                payload={
                    "work_item_id": WORK_ITEM_ID,
                    "summary": "Live external adapter validation remains outside the deterministic local dogfood run.",
                },
            ).call_id,
            _write_index(
                self.tools,
                self.role_instance_id,
                "active",
                "engineering",
                "Engineering evidence recorded; QA handoff required.",
            ),
            self.tools.call(
                role_instance_id=self.role_instance_id,
                tool_name="handoff.require",
                payload=_handoff_payload(
                    target_role="qa-engineer",
                    phase="testing",
                    accountable_role="qa-engineer",
                    required_next_action="Review dogfood implementation evidence and hand to Release Manager if accepted.",
                    consulted_roles=["release-manager"],
                    informed_roles=["project-manager", "delivery-manager"],
                ),
            ).call_id,
            self.tools.call(
                role_instance_id=self.role_instance_id,
                tool_name="status.complete",
                payload={"summary": "Engineering evidence recorded and QA handoff sent."},
            ).call_id,
        ]
        return calls


@dataclass
class _SolutionArchitectWorker:
    tools: V3ToolService
    project_id: str

    @property
    def role_instance_id(self) -> str:
        return f"{self.project_id}.solution-architect.1"

    def run(self, prompt: str, message: AgentMessage) -> list[str]:
        del prompt, message
        return [
            self.tools.call(
                role_instance_id=self.role_instance_id,
                tool_name="decision.record",
                payload={
                    "work_item_id": WORK_ITEM_ID,
                    "summary": "Solution Architecture confirms the proof uses role-service inboxes and safe-output tools.",
                    "target_ref": f"work-items/{WORK_ITEM_ID}/index.md",
                },
            ).call_id,
            self.tools.call(
                role_instance_id=self.role_instance_id,
                tool_name="status.complete",
                payload={"summary": "Architecture consultation recorded."},
            ).call_id,
        ]


@dataclass
class _QaWorker:
    tools: V3ToolService
    project_id: str

    @property
    def role_instance_id(self) -> str:
        return f"{self.project_id}.qa-engineer.1"

    def run(self, prompt: str, message: AgentMessage) -> list[str]:
        del prompt, message
        return [
            self.tools.call(
                role_instance_id=self.role_instance_id,
                tool_name="work_item.update_state",
                payload={
                    "work_item_id": WORK_ITEM_ID,
                    "state": "active",
                    "owner_role": "qa-engineer",
                    "current_phase": "testing",
                    "next_action": "QA accepted evidence; Release Manager handoff required.",
                },
            ).call_id,
            self.tools.call(
                role_instance_id=self.role_instance_id,
                tool_name="decision.record",
                payload={
                    "work_item_id": WORK_ITEM_ID,
                    "summary": "QA accepted the role-service dogfood evidence for local release.",
                    "target_ref": f"work-items/{WORK_ITEM_ID}/index.md",
                },
            ).call_id,
            _write_index(self.tools, self.role_instance_id, "active", "qa-engineer", "QA accepted evidence; release handoff required."),
            self.tools.call(
                role_instance_id=self.role_instance_id,
                tool_name="handoff.require",
                payload=_handoff_payload(
                    target_role="release-manager",
                    phase="deployment",
                    accountable_role="release-manager",
                    required_next_action="Deploy the configured target, record release evidence, and close the release.",
                    consulted_roles=["qa-engineer"],
                    informed_roles=["project-manager", "delivery-manager", "product-manager"],
                ),
            ).call_id,
            self.tools.call(
                role_instance_id=self.role_instance_id,
                tool_name="status.complete",
                payload={"summary": "QA accepted evidence and handed to Release Manager."},
            ).call_id,
        ]


@dataclass
class _ReleaseManagerWorker:
    tools: V3ToolService
    project_id: str
    deployment_target_id: str

    @property
    def role_instance_id(self) -> str:
        return f"{self.project_id}.release-manager.1"

    def run(self, prompt: str, message: AgentMessage) -> list[str]:
        del prompt, message
        return [
            self.tools.call(
                role_instance_id=self.role_instance_id,
                tool_name="work_item.update_state",
                payload={
                    "work_item_id": WORK_ITEM_ID,
                    "state": "release_review",
                    "owner_role": "release-manager",
                    "current_phase": "deployment",
                    "next_action": f"Deploying `{self.deployment_target_id}`.",
                },
            ).call_id,
            self.tools.call(
                role_instance_id=self.role_instance_id,
                tool_name="release.deploy",
                payload={
                    "release_id": RELEASE_ID,
                    "work_item_id": WORK_ITEM_ID,
                    "target_id": self.deployment_target_id,
                    "scope": "Local V3 agent-service dogfood release smoke.",
                    "version_ref": "local-agent-service-dogfood",
                    "approval_ref": PRODUCT_APPROVAL_ID,
                    "smoke_evidence": f"Deployment target `{self.deployment_target_id}` completed.",
                    "residual_risks": "Live Teams, OneDrive, and NATS credentials still require environment-specific validation.",
                },
            ).call_id,
            self.tools.call(
                role_instance_id=self.role_instance_id,
                tool_name="release.close",
                payload={
                    "work_item_id": WORK_ITEM_ID,
                    "closure_owner_role": "project-manager",
                    "closure_note": "Release closed after local agent-service deployment smoke.",
                },
            ).call_id,
            _write_index(self.tools, self.role_instance_id, "closed", "project-manager", "Release deployed and closed; Project Manager closure update required."),
            self.tools.call(
                role_instance_id=self.role_instance_id,
                tool_name="informed.update",
                payload={
                    "work_item_id": WORK_ITEM_ID,
                    "target_role": "project-manager",
                    "message": "Release deployed and closed; update backlog and notify sponsor.",
                },
            ).call_id,
            self.tools.call(
                role_instance_id=self.role_instance_id,
                tool_name="status.complete",
                payload={"summary": "Release deployed and closed; Project Manager informed."},
            ).call_id,
        ]


@dataclass
class _ProjectManagerWorker:
    tools: V3ToolService
    project_id: str
    sponsor_contact: DogfoodSponsorContact | None

    @property
    def role_instance_id(self) -> str:
        return f"{self.project_id}.project-manager.1"

    def run(self, prompt: str, message: AgentMessage) -> list[str]:
        del prompt
        calls = [
            self.tools.call(
                role_instance_id=self.role_instance_id,
                tool_name="backlog.upsert",
                payload={
                    "queue_item_id": QUEUE_ITEM_ID,
                    "title": "V3 agent-service dogfood release",
                    "summary": "Tiny V3 slice completed through role services, deployment, release, and closure.",
                    "status": "closed",
                    "owner_role": "project-manager",
                    "linked_work_item_id": WORK_ITEM_ID,
                    "source_ref": "teams:msg-v3-agent-service-e2e",
                },
            ).call_id,
            self.tools.call(
                role_instance_id=self.role_instance_id,
                tool_name="document.write_root_work_item_index",
                payload={},
            ).call_id,
        ]
        if self.sponsor_contact is not None:
            calls.append(
                self.tools.call(
                    role_instance_id=self.role_instance_id,
                    tool_name="messaging.send",
                    payload={
                        "connector": self.sponsor_contact.connector,
                        "target_ref": self.sponsor_contact.target_ref,
                        "thread_ref": self.sponsor_contact.thread_ref,
                        "work_item_id": WORK_ITEM_ID,
                        "text_markdown": (
                            "**V3 agent-service dogfood release closed**\n\n"
                            f"`{WORK_ITEM_ID}` reached closure after role-service processing, "
                            "QA acceptance, deployment, release evidence, and document-index updates."
                        ),
                    },
                ).call_id
            )
        calls.append(_status_reply(self.tools, self.role_instance_id, message, "Project closure recorded and sponsor notification sent."))
        return calls


def _run_role(
    *,
    db: V3Database,
    broker: BrokerAdapter,
    stream: str,
    runtime_state_dir: Path,
    project_id: str,
    role_id: str,
    worker: object,
) -> None:
    service = RoleAgentService(
        config=_role_config(runtime_state_dir, project_id=project_id, role_id=role_id, stream=stream),
        broker=broker,
        worker=worker,  # type: ignore[arg-type]
        memory=DatabaseRoleMemory(db),
        conversation_context=DatabaseConversationContext(db),
        work_item_governance_context=DatabaseWorkItemGovernanceContextProvider(db),
        status_reporter=DatabaseAgentStatusReporter(db),
        terminal_tool_call_audit=DatabaseTerminalToolCallAudit(db),
        run_recorder=DatabaseAgentRunRecorder(db),
        max_delivery_attempts=1,
    )
    result = service.run_once()
    if result is None:
        raise RuntimeError(f"{role_id} had no inbox message to process")
    if result.status != "completed":
        raise RuntimeError(f"{role_id} failed dogfood run: {result.error or result.status}")


def _role_config(runtime_state_dir: Path, *, project_id: str, role_id: str, stream: str) -> RoleInstanceConfig:
    role_dir = runtime_state_dir / "agent-configs" / role_id
    role_dir.mkdir(parents=True, exist_ok=True)
    role_prompt = role_dir / "role.md"
    if not role_prompt.exists():
        role_prompt.write_text(f"You are the `{role_id}` role agent for the V3 dogfood proof.", encoding="utf-8")
    return RoleInstanceConfig(
        project_id=project_id,
        role_id=role_id,
        instance_id="1",
        role_prompt_path=role_prompt,
        memory_db_path=runtime_state_dir / "memory" / f"{role_id}.sqlite3",
        inbox_stream=stream,
        inbox_consumer=f"{role_id}.1",
    )


def _record_product_approval(
    *,
    db: V3Database,
    broker: BrokerAdapter,
    stream: str,
    sponsor_contact: DogfoodSponsorContact | None,
) -> None:
    approval_message = StakeholderMessage(
        connector=sponsor_contact.connector if sponsor_contact is not None else "teams",
        message_id="msg-v3-agent-service-product-approval",
        source_type="dm",
        sender_ref=sponsor_contact.responder_ref if sponsor_contact is not None else "sponsor",
        conversation_ref="dm:product-manager",
        text=f"{PRODUCT_APPROVAL_ID} approved",
        reply_target_ref=sponsor_contact.target_ref if sponsor_contact is not None else None,
        reply_thread_ref=sponsor_contact.thread_ref if sponsor_contact is not None else None,
    )
    DatabaseApprovalResponseRecorder(db.path, broker=broker, stream=stream).record(approval_message)


def _handoff_payload(
    *,
    target_role: str,
    phase: str,
    accountable_role: str,
    required_next_action: str,
    consulted_roles: list[str],
    informed_roles: list[str],
) -> dict[str, object]:
    return {
        "work_item_id": WORK_ITEM_ID,
        "target_role": target_role,
        "phase": phase,
        "accountable_role": accountable_role,
        "required_next_action": required_next_action,
        "acceptance_criteria": ["The role records factual evidence and hands off only when ready."],
        "evidence_requirements": [f"work-items/{WORK_ITEM_ID}/index.md"],
        "artifact_links": [f"work-items/{WORK_ITEM_ID}/index.md"],
        "open_decisions": [],
        "open_risks": ["Live external adapters remain environment-specific."],
        "consulted_roles": consulted_roles,
        "informed_roles": informed_roles,
        "stakeholder_follow_up": [],
    }


def _write_index(
    tools: V3ToolService,
    role_instance_id: str,
    status: str,
    owner_role: str,
    governance_state: str,
) -> str:
    return tools.call(
        role_instance_id=role_instance_id,
        tool_name="document.write_work_item_index",
        payload={
            "work_item_id": WORK_ITEM_ID,
            "title": "V3 agent-service dogfood release",
            "status": status,
            "owner_role": owner_role,
            "raci_summary": "Agent-service proof follows V3 SDLC RACI through role-owned handoffs.",
            "governance_state": governance_state,
            "next_action": governance_state,
            "consultations": ["Solution Architecture consultation recorded."],
            "approvals": [f"{PRODUCT_APPROVAL_ID} approved."],
            "evidence": ["Role-service safe-output calls recorded evidence for each phase."],
            "decisions": ["Continue V3 through role-owned broker inbox processing."],
            "risks": ["Live external adapter validation remains environment-specific."],
        },
    ).call_id


def _status_reply(tools: V3ToolService, role_instance_id: str, message: AgentMessage, text: str) -> str:
    payload: dict[str, object] = {"text_markdown": text}
    connector = message.payload.get("connector")
    target_ref = message.payload.get("reply_target_ref")
    thread_ref = message.payload.get("reply_thread_ref")
    if connector and target_ref:
        payload.update({"connector": str(connector), "target_ref": str(target_ref)})
        if thread_ref:
            payload["thread_ref"] = str(thread_ref)
    return tools.call(role_instance_id=role_instance_id, tool_name="status.reply", payload=payload).call_id


def _complete_noop(tools: V3ToolService, role_instance_id: str, reason: str) -> list[str]:
    return [
        tools.call(role_instance_id=role_instance_id, tool_name="noop", payload={"reason": reason}).call_id,
        tools.call(role_instance_id=role_instance_id, tool_name="status.complete", payload={"summary": reason}).call_id,
    ]
