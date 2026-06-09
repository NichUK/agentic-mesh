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
    role_profile: str = ""
    accountabilities: list[str] = field(default_factory=list)
    decision_rights: dict[str, list[str]] = field(default_factory=dict)
    boundaries: list[str] = field(default_factory=list)
    collaboration_style: list[str] = field(default_factory=list)
    quality_bar: list[str] = field(default_factory=list)
    memory_focus: list[str] = field(default_factory=list)
    core_workflows: list[dict[str, Any]] = field(default_factory=list)
    standards_references: list[dict[str, str]] = field(default_factory=list)
    anti_patterns: list[str] = field(default_factory=list)
    capabilities: "CapabilityProfileConfig" = field(
        default_factory=lambda: CapabilityProfileConfig()
    )


@dataclass(frozen=True)
class CapabilityValidationConfig:
    kind: str
    command: list[str] = field(default_factory=list)
    target: str | None = None
    freshness_seconds: int = 86400
    timeout_seconds: int = 5


@dataclass(frozen=True)
class CapabilityFallbackConfig:
    owner: str
    policy_summary: str
    residual_impact: str
    review_point: str | None = None
    allowed_scope: str | None = None


@dataclass(frozen=True)
class CapabilityWaiverConfig:
    owner: str
    reason: str
    residual_impact: str
    expires_at: str | None = None
    review_point: str | None = None


@dataclass(frozen=True)
class CapabilityAffectsConfig:
    lifecycle_states: list[str] = field(default_factory=list)
    work_item_types: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CapabilityConfig:
    capability_id: str
    category: str
    requirement: str
    display_name: str
    description: str = ""
    validation: CapabilityValidationConfig | None = None
    affects: CapabilityAffectsConfig = field(default_factory=CapabilityAffectsConfig)
    fallback: CapabilityFallbackConfig | None = None
    waiver: CapabilityWaiverConfig | None = None
    severity: str = "medium"
    impact: str = ""
    next_action: str = ""
    action_owner: str = ""
    configured_source: str = ""
    source_path: str = ""


@dataclass(frozen=True)
class CapabilityProfileConfig:
    schema_version: str = "role-capability-profile-v0"
    capabilities: list[CapabilityConfig] = field(default_factory=list)


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
    capability_defaults: CapabilityProfileConfig = field(
        default_factory=CapabilityProfileConfig
    )


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
    sandbox_mode: str = "workspace-write"
    auth: AuthBinding | None = None
    timeout_seconds: int | None = None
    progress_window_seconds: int | None = None
    max_timeout_seconds: int | None = None


@dataclass(frozen=True)
class FlowHandoff:
    status: str
    target_state: str
    target_role: str
    message_type: str
    target_mesh: str | None = None
    create_work_item: bool = False


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
    affected_roles: list[str] = field(default_factory=list)
    review_outcomes: list[str] = field(default_factory=list)
    max_resolution_loops: int | None = None
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
class FlowVisualizationConfig:
    enabled: bool = True
    default_format: str = "mermaid"
    group_by: list[str] = field(default_factory=lambda: ["mesh", "role"])


@dataclass(frozen=True)
class SdlcFlow:
    flow_id: str
    entry_state: str
    work_item_types: list[str]
    states: dict[str, FlowState]
    sponsor_initiated_work: SponsorInitiatedWorkPolicy | None = None
    visualization: FlowVisualizationConfig = field(default_factory=FlowVisualizationConfig)


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
class DocumentLibraryConfig:
    backend: str = "filesystem"
    root: str = "docs"
    structure_policy: str = "togaf-sdlc-v1"
    index_path: str = "00-index/document-library-manifest.json"
    review_log_standard: str = "same-document-review-log-v1"
    versioning: str = "backend"
    retention_policy: str | None = None


@dataclass(frozen=True)
class RoleMemoryConfig:
    enabled: bool = True
    backend: str = "filesystem"
    root: str = "memory/roles"
    provenance_required: bool = True
    refresh_from_document_library: bool = True
    team_overlay_root: str = "memory/team-overlays"


@dataclass(frozen=True)
class ProjectMeshConfig:
    mesh_id: str
    name: str
    flow: str
    roles: list[str]
    parent_mesh: str | None = None
    purpose: str | None = None


@dataclass(frozen=True)
class ProjectRoleOverride:
    role_id: str
    template: str
    instances: int
    worker: WorkerConfig
    instructions: list[str]
    write_paths: list[str]
    channels: dict[str, str]
    capabilities: CapabilityProfileConfig = field(default_factory=CapabilityProfileConfig)


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
class ProjectGoalConfig:
    description: str = ""
    success_measures: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    guidance: list[str] = field(default_factory=list)


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
class GatewayBotConfig:
    display_name: str
    bot_id_ref: str
    secret_ref: str


@dataclass(frozen=True)
class GatewayTeamsConfig:
    connector: str
    bot: GatewayBotConfig
    dm_enabled: bool = True
    intake_channels: list[str] = field(default_factory=list)
    process_role_channels_by_default: bool = False
    fallback_surface: str = "status_fallback"
    approvals_surface: str = "approvals"


@dataclass(frozen=True)
class GatewayConfig:
    gateway_id: str
    enabled: bool = True
    no_delivery_work: bool = True
    raw_retention_mode: str = "reference_only"
    default_owner_role: str = "delivery-manager"
    teams: GatewayTeamsConfig | None = None
    authorization_policy: str = "gateway-v0-default"


@dataclass(frozen=True)
class NotificationSurfaceConfig:
    surface_id: str
    connector: str
    route: str
    label: str


@dataclass(frozen=True)
class NotificationEventOverrideConfig:
    visibility: str
    preferred_surface: str | None = None


@dataclass(frozen=True)
class NotificationCompatibilityConfig:
    role_channels_enabled: bool = True
    role_channels_default_visibility: str = "dashboard_only"


@dataclass(frozen=True)
class NotificationPolicyConfig:
    schema_version: str = "notification-policy-v0"
    defaults: dict[str, str] = field(default_factory=dict)
    surfaces: dict[str, NotificationSurfaceConfig] = field(default_factory=dict)
    event_overrides: dict[str, NotificationEventOverrideConfig] = field(default_factory=dict)
    role_overrides: dict[str, dict[str, NotificationEventOverrideConfig]] = field(
        default_factory=dict
    )
    compatibility: NotificationCompatibilityConfig = field(
        default_factory=NotificationCompatibilityConfig
    )


@dataclass(frozen=True)
class ControlPlanePolicyConfig:
    schema_version: str = "control-plane-policy-v0"
    binding_mode: str = "local_private"
    api_enabled: bool = False
    mcp_enabled: bool = False
    remote_promotion_enabled: bool = False
    support_read_enabled: bool = False
    safe_external_base_url: str | None = None


@dataclass(frozen=True)
class HumanGatePolicyConfig:
    schema_version: str = "human-gate-policy-v0"
    sponsor_approval_before_build: str = "not_required"
    work_item_types: list[str] = field(default_factory=list)
    gate_id: str = "pre_implementation_sponsor_approval"
    response_type: str = "approve_not_approve"
    requested_from: str = "release-sponsor"
    channel: str = "approvals"


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
    goal: ProjectGoalConfig = field(default_factory=ProjectGoalConfig)
    document_library: DocumentLibraryConfig = field(default_factory=DocumentLibraryConfig)
    role_memory: RoleMemoryConfig = field(default_factory=RoleMemoryConfig)
    meshes: dict[str, ProjectMeshConfig] = field(default_factory=dict)
    auth_credentials: dict[str, AuthCredential] = field(default_factory=dict)
    connectors: dict[str, ProjectConnectorConfig] = field(default_factory=dict)
    gateways: dict[str, GatewayConfig] = field(default_factory=dict)
    notification_policy: NotificationPolicyConfig = field(
        default_factory=NotificationPolicyConfig
    )
    control_plane: ControlPlanePolicyConfig = field(
        default_factory=ControlPlanePolicyConfig
    )
    human_gate_policy: HumanGatePolicyConfig = field(
        default_factory=HumanGatePolicyConfig
    )
    capability_defaults: CapabilityProfileConfig = field(
        default_factory=CapabilityProfileConfig
    )


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
    purpose: str | None = None
    review_status: str | None = None
    index_summary: str | None = None
    maintain_work_item_index: bool | None = None


@dataclass(frozen=True)
class Handoff:
    target_role: str
    message_type: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class RouteRequest:
    target_role: str
    message_type: str
    payload: dict[str, Any]
    origin: str = "route"


@dataclass(frozen=True)
class AgentRunResult:
    status: str
    message: str
    document_updates: list[DocumentUpdate] = field(default_factory=list)
    routes: list[RouteRequest] = field(default_factory=list)
    handoffs: list[Handoff] = field(default_factory=list)
