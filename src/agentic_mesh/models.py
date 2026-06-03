from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


@dataclass(frozen=True)
class NamingDefaults:
    brand_prefix: str
    service_name_template: str
    bot_display_name_template: str
    resource_namespace: str


@dataclass(frozen=True)
class RoleTemplate:
    role_id: str
    version: int
    purpose: str
    standing_instructions: list[str]
    default_tools: list[str]
    documentation_obligations: list[str]
    handoff_targets: list[str]


@dataclass(frozen=True)
class OrganizationConfig:
    organization_id: str
    name: str
    global_language: str
    global_locale: str
    naming_defaults: NamingDefaults
    documentation_defaults: dict[str, Any]
    conversation_defaults: dict[str, Any]
    work_intake_defaults: dict[str, Any]
    handoff_defaults: dict[str, Any]
    security_defaults: dict[str, Any]


@dataclass(frozen=True)
class AuthMethod:
    method_id: str
    category: str
    applies_to: list[str]
    env_vars: list[str]
    requires_secret_ref: bool
    requires_mount_ref: bool
    description: str


@dataclass(frozen=True)
class AuthBinding:
    method: str
    secret_ref: str | None = None
    mount_ref: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    notes: str | None = None
    credential_ref: str | None = None


@dataclass(frozen=True)
class AuthCredential:
    credential_id: str
    method: str
    secret_ref: str | None = None
    mount_ref: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    notes: str | None = None


@dataclass(frozen=True)
class WorkerConfig:
    adapter: str
    model: str
    reasoning_effort: str = "medium"
    auth: AuthBinding | None = None


@dataclass(frozen=True)
class FlowHandoff:
    status: str
    target_state: str
    target_role: str
    message_type: str


@dataclass(frozen=True)
class FlowConsult:
    consult_id: str
    target_state: str
    target_role: str
    message_type: str
    purpose: str


@dataclass(frozen=True)
class FlowGate:
    gate_id: str
    type: str
    required_documents: list[str] = field(default_factory=list)
    required_review_status: str | None = None
    reviewer_role: str | None = None
    response_type: str | None = None
    prompt: str | None = None
    requested_from: str | None = None
    channel: str | None = None
    timeout: str | None = None
    on_timeout: str | None = None
    completion_criteria: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FlowState:
    state_id: str
    owner_role: str
    purpose: str
    artifact_path: str
    handoffs: dict[str, FlowHandoff]
    consults: dict[str, FlowConsult] = field(default_factory=dict)
    gates: list[FlowGate] = field(default_factory=list)


@dataclass(frozen=True)
class SponsorInitiatedWorkPolicy:
    allow_from_any_state: bool
    default_work_item_type: str
    default_intake_state: str
    capture_rule: str
    routing_rule: str
    completion_rule: str


@dataclass(frozen=True)
class SdlcFlow:
    flow_id: str
    entry_state: str
    work_item_types: list[str]
    states: dict[str, FlowState]
    sponsor_initiated_work: SponsorInitiatedWorkPolicy | None = None


@dataclass(frozen=True)
class ResponseTypeTemplate:
    response_type_id: str
    label: str
    description: str
    input_mode: str
    value_type: str
    options: list[dict[str, Any]] = field(default_factory=list)
    validation: dict[str, Any] = field(default_factory=dict)
    ui_hints: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DocumentAccountability:
    path: str
    owner_role: str
    accountability: str
    can_edit_contributions: bool
    review_on_contribution: bool
    required_sections: list[str]
    contributing_roles: list[str]
    lifecycle_events: list[str]


@dataclass(frozen=True)
class ProjectRoleOverride:
    role_id: str
    template: str
    instances: int
    worker: WorkerConfig
    instructions: list[str]
    write_paths: list[str]
    channels: dict[str, str]


@dataclass(frozen=True)
class ProjectRepositoryConfig:
    repository_id: str
    path: str
    type: str = "git"
    default_branch: str | None = None
    remote: str | None = None


@dataclass(frozen=True)
class ProjectWorkspaceConfig:
    root: str
    default_repository: str
    repositories: dict[str, ProjectRepositoryConfig]


@dataclass(frozen=True)
class ConnectorChannelConfig:
    channel_id: str
    name: str


@dataclass(frozen=True)
class TeamsRoleBotConfig:
    role_id: str
    display_name: str
    bot_id_ref: str
    secret_ref: str


@dataclass(frozen=True)
class ConnectorIngressConfig:
    public_endpoint: str
    listen_host: str
    listen_port: int
    path: str


@dataclass(frozen=True)
class ProjectConnectorConfig:
    connector_id: str
    adapter: str
    identity_model: str
    tenant_id: str | None
    team_id: str
    team_name: str
    channels: dict[str, ConnectorChannelConfig]
    role_bots: dict[str, TeamsRoleBotConfig] = field(default_factory=dict)
    ingress: ConnectorIngressConfig | None = None


@dataclass(frozen=True)
class RoleInstanceConfig:
    instance_id: str
    project_id: str
    role_id: str
    ordinal: int
    template: RoleTemplate
    override: ProjectRoleOverride
    service_name: str | None = None

    @property
    def telemetry_service_name(self) -> str:
        return self.service_name or f"{self.project_id}.{self.role_id}.{self.ordinal}"


@dataclass(frozen=True)
class ProjectConfig:
    project_id: str
    name: str
    workspace: ProjectWorkspaceConfig
    roles: dict[str, ProjectRoleOverride]
    document_accountabilities: dict[str, DocumentAccountability]
    flow: SdlcFlow
    auth_credentials: dict[str, AuthCredential] = field(default_factory=dict)
    connectors: dict[str, ProjectConnectorConfig] = field(default_factory=dict)


@dataclass(frozen=True)
class MeshConfig:
    organization: OrganizationConfig
    auth_methods: dict[str, AuthMethod]
    response_types: dict[str, ResponseTypeTemplate]
    project: ProjectConfig
    role_templates: dict[str, RoleTemplate]
    instances: dict[str, RoleInstanceConfig]


@dataclass(frozen=True)
class Message:
    message_id: str
    role_id: str
    type: str
    payload: dict[str, Any]
    correlation_id: str
    created_at: str
    source: str
    claimed_by: str | None = None
    claimed_at: str | None = None
    trace_context: dict[str, str] = field(default_factory=dict)

    @staticmethod
    def create(
        role_id: str,
        message_type: str,
        payload: dict[str, Any],
        source: str,
        correlation_id: str | None = None,
        trace_context: dict[str, str] | None = None,
    ) -> "Message":
        correlation = correlation_id or new_id("corr")
        return Message(
            message_id=new_id("msg"),
            role_id=role_id,
            type=message_type,
            payload=payload,
            correlation_id=correlation,
            created_at=utc_now_iso(),
            source=source,
            trace_context=dict(trace_context or {}),
        )

    def claimed(self, instance_id: str) -> "Message":
        return Message(
            message_id=self.message_id,
            role_id=self.role_id,
            type=self.type,
            payload=self.payload,
            correlation_id=self.correlation_id,
            created_at=self.created_at,
            source=self.source,
            claimed_by=instance_id,
            claimed_at=utc_now_iso(),
            trace_context=dict(self.trace_context),
        )


@dataclass(frozen=True)
class ConnectorMessage:
    message_id: str
    channel: str
    type: str
    payload: dict[str, Any]
    correlation_id: str
    created_at: str
    source: str
    claimed_by: str | None = None
    claimed_at: str | None = None
    trace_context: dict[str, str] = field(default_factory=dict)

    @staticmethod
    def create(
        channel: str,
        message_type: str,
        payload: dict[str, Any],
        source: str,
        correlation_id: str | None = None,
        trace_context: dict[str, str] | None = None,
    ) -> "ConnectorMessage":
        correlation = correlation_id or new_id("corr")
        return ConnectorMessage(
            message_id=new_id("conn-msg"),
            channel=channel,
            type=message_type,
            payload=payload,
            correlation_id=correlation,
            created_at=utc_now_iso(),
            source=source,
            trace_context=dict(trace_context or {}),
        )

    def claimed(self, connector_id: str) -> "ConnectorMessage":
        return ConnectorMessage(
            message_id=self.message_id,
            channel=self.channel,
            type=self.type,
            payload=self.payload,
            correlation_id=self.correlation_id,
            created_at=self.created_at,
            source=self.source,
            claimed_by=connector_id,
            claimed_at=utc_now_iso(),
            trace_context=dict(self.trace_context),
        )


@dataclass(frozen=True)
class DocumentUpdate:
    path: str
    content: str


@dataclass(frozen=True)
class Handoff:
    target_role: str
    message_type: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class AgentRunResult:
    status: str
    message: str
    document_updates: list[DocumentUpdate] = field(default_factory=list)
    handoffs: list[Handoff] = field(default_factory=list)
