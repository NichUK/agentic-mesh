from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime
from functools import partial
import math
import os
from pathlib import Path
import re
from typing import Any, Callable, Literal
import uuid

import anyio
from fastapi import Depends, FastAPI, Header, Query, Request, Response, Security, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, model_validator
import psycopg
from psycopg.rows import dict_row
from starlette.background import BackgroundTask

from agentic_mesh_v5 import __version__
from agentic_mesh_v5.api_auth import Authorizer
from agentic_mesh_v5.api_auth import Principal
from agentic_mesh_v5.api_auth import authorizer_from_environment
from agentic_mesh_v5.database import DatabaseConfigurationError
from agentic_mesh_v5.database import DatabaseError
from agentic_mesh_v5.database import SCHEMA
from agentic_mesh_v5.database import database_url_from_environment
from agentic_mesh_v5.docker_fleet import DockerContainerFleetSupervisor
from agentic_mesh_v5.dashboard_reads import DashboardReadStore
from agentic_mesh_v5.dashboard_reads import usage_traffic
from agentic_mesh_v5.continuation import ContinuationAuthorizationError
from agentic_mesh_v5.continuation import ContinuationConflict
from agentic_mesh_v5.continuation import ContinuationMonitor
from agentic_mesh_v5.config_activation import ConfigActivationError
from agentic_mesh_v5.config_activation import ConfigActivationStore
from agentic_mesh_v5.config_promotions import ConfigPromotionConflict
from agentic_mesh_v5.config_promotions import ConfigPromotionError
from agentic_mesh_v5.config_promotions import ConfigPromotionNotFound
from agentic_mesh_v5.config_promotions import ConfigPromotionStore
from agentic_mesh_v5.document_store import DocumentConflict
from agentic_mesh_v5.document_store import DocumentNotFound
from agentic_mesh_v5.document_store import DocumentPermissionDenied
from agentic_mesh_v5.document_store import DocumentStore
from agentic_mesh_v5.document_store import DocumentUnavailable
from agentic_mesh_v5.document_store import HttpxTransport
from agentic_mesh_v5.document_store import MountedAccessTokenProvider
from agentic_mesh_v5.document_store import OneDriveDocumentStoreFactory
from agentic_mesh_v5.d8a_proxy import D8AProxy
from agentic_mesh_v5.d8a_proxy import D8AProxyError
from agentic_mesh_v5.fleet import FleetConflict
from agentic_mesh_v5.fleet import FleetNotFound
from agentic_mesh_v5.fleet import FleetScaler
from agentic_mesh_v5.fleet import FleetSupervisor
from agentic_mesh_v5.fleet import FleetSupervisorError
from agentic_mesh_v5.fleet import ScalingPolicy
from agentic_mesh_v5.flow_definition import FlowDefinition
from agentic_mesh_v5.flow_definition import FlowDefinitionError
from agentic_mesh_v5.flow_definition import load_flow
from agentic_mesh_v5.flow_engine import FlowEngine
from agentic_mesh_v5.flow_engine import FlowEngineConflict
from agentic_mesh_v5.flow_engine import FlowEngineNotFound
from agentic_mesh_v5.flow_engine import TransitionSource
from agentic_mesh_v5.governance import GovernanceConflict
from agentic_mesh_v5.governance import GovernanceNotFound
from agentic_mesh_v5.governance import GovernanceStore
from agentic_mesh_v5.health import HealthReporter
from agentic_mesh_v5.handoffs import HandoffAuthorizationError
from agentic_mesh_v5.handoffs import HandoffConflict
from agentic_mesh_v5.handoffs import HandoffNotFound
from agentic_mesh_v5.handoffs import HandoffOffer
from agentic_mesh_v5.handoffs import HandoffStore
from agentic_mesh_v5.lifecycle import LifecycleAuthorizationError
from agentic_mesh_v5.lifecycle import LifecycleConflict
from agentic_mesh_v5.lifecycle import LifecycleNotFound
from agentic_mesh_v5.lifecycle import LifecycleStore
from agentic_mesh_v5.package_resolver import resolve_packages
from agentic_mesh_v5.package_resolver import PackageResolutionError
from agentic_mesh_v5.progress import ProgressConflict
from agentic_mesh_v5.progress import ProgressDraft
from agentic_mesh_v5.progress import ProgressNotFound
from agentic_mesh_v5.progress import ProgressStore
from agentic_mesh_v5.project_manifest import ProjectManifestStore
from agentic_mesh_v5.queues import LeaseExpired
from agentic_mesh_v5.queues import QueueAuthorizationError
from agentic_mesh_v5.queues import QueueConflict
from agentic_mesh_v5.queues import QueueNotFound
from agentic_mesh_v5.queues import RoleQueueStore
from agentic_mesh_v5.read_models import ReadModelNotFound
from agentic_mesh_v5.read_models import ReadModelStore
from agentic_mesh_v5.recovery_supervisor import RecoverySupervisorStore
from agentic_mesh_v5.reliability import ReliabilityConflict
from agentic_mesh_v5.reliability import ReliabilityNotFound
from agentic_mesh_v5.reliability import ReliabilityStore
from agentic_mesh_v5.routing import RouteDraft
from agentic_mesh_v5.routing import Router
from agentic_mesh_v5.routing import RoutingConflict
from agentic_mesh_v5.routing import RoutingNotFound
from agentic_mesh_v5.sponsor_approvals import SponsorApprovalCoordinator
from agentic_mesh_v5.telemetry import Telemetry
from agentic_mesh_v5.telemetry import telemetry_from_environment
from agentic_mesh_v5.usage import UsageNotFound
from agentic_mesh_v5.usage import UsageStore


API_PREFIX = "/api/v1"
CONFIG_ROOT_ENV = "AGENTIC_MESH_V5_CONFIG_ROOT"
DOCUMENT_CREDENTIAL_ROOT_ENV = "AGENTIC_MESH_V5_DOCUMENT_CREDENTIAL_ROOT"
DOCUMENT_ROOT_ID_ENV = "AGENTIC_MESH_V5_DOCUMENT_ROOT_ID"
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
IDENTIFIER_PATTERN = r"^[A-Za-z0-9._:-]{1,128}$"


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Problem(ApiModel):
    type: str
    title: str
    status: int
    detail: str
    instance: str
    request_id: str
    errors: list[dict[str, Any]] | None = None


class DependencyHealthResponse(ApiModel):
    name: str
    status: Literal["ok", "degraded", "unavailable"]
    reason: str


class HealthResponse(ApiModel):
    runtime: str
    version: str
    kind: Literal["liveness", "readiness"]
    status: Literal["ok", "degraded"]
    checked_at: str
    schema_version: int | None
    available_schema_version: int | None
    dependencies: list[DependencyHealthResponse]


class ProjectCreate(ApiModel):
    project_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    sponsor_ids: list[str] = Field(min_length=1)


class ProjectResponse(ApiModel):
    project_id: str
    display_name: str
    status: str
    sponsor_ids: list[str]


class WorkItemCreate(ApiModel):
    work_item_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    owner_role_id: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)


class WorkItemTransition(ApiModel):
    target_status: Literal["active", "completed", "error"]
    expected_version: int = Field(ge=1)
    correlation_id: str = Field(min_length=1)
    reason: str = ""
    evidence: dict[str, Any] = Field(default_factory=dict)


class WorkItemResponse(ApiModel):
    project_id: str
    work_item_id: str
    title: str
    status: str
    owner_role_id: str
    version: int
    terminal_reason: str | None
    terminal_evidence: dict[str, Any]


class FlowStart(ApiModel):
    operation_id: str = Field(pattern=IDENTIFIER_PATTERN)
    fields: dict[str, Any] = Field(default_factory=dict)


class FlowDispatch(ApiModel):
    kind: Literal["consult", "inform"]
    obligation_id: str = Field(pattern=IDENTIFIER_PATTERN)


class ArtifactVerify(ApiModel):
    path: str = Field(min_length=1, max_length=4000)
    record_id: str = Field(pattern=IDENTIFIER_PATTERN)


class ConsultationRecord(ApiModel):
    obligation_id: str = Field(pattern=IDENTIFIER_PATTERN)
    decision: Literal["responded", "exception"]
    record_id: str = Field(pattern=IDENTIFIER_PATTERN)
    reason: str = Field(default="", max_length=4000)
    evidence: dict[str, Any] = Field(default_factory=dict)


class GovernanceGateDecision(ApiModel):
    obligation_id: str = Field(pattern=IDENTIFIER_PATTERN)
    decision: Literal["approved", "rejected", "exception"]
    record_id: str = Field(pattern=IDENTIFIER_PATTERN)
    reason: str = Field(default="", max_length=4000)
    evidence: dict[str, Any] = Field(default_factory=dict)
    architecture_impact: Literal["no-material", "material", "uncertain"] | None = None


class FlowTransitionPrepare(ApiModel):
    outcome: str = Field(pattern=IDENTIFIER_PATTERN)
    fields: dict[str, Any] = Field(default_factory=dict)
    source_lease_id: str = Field(pattern=IDENTIFIER_PATTERN)
    source_lease_token: str = Field(min_length=1, max_length=512)
    expected_version: int = Field(ge=1)
    operation_id: str = Field(pattern=IDENTIFIER_PATTERN)


class FlowTransitionAction(ApiModel):
    expected_version: int = Field(ge=1)
    operation_id: str = Field(pattern=IDENTIFIER_PATTERN)


class FlowComplete(FlowTransitionAction):
    evidence: dict[str, Any]

    @model_validator(mode="after")
    def require_evidence(self):
        if not self.evidence:
            raise ValueError("completion evidence is required")
        return self


class FlowRunResponse(ApiModel):
    project_id: str
    work_item_id: str
    flow_id: str
    flow_digest: str
    current_state: str
    owner_role_id: str
    status: str
    fields: dict[str, Any]
    version: int
    pending_route_id: str | None
    pending_target_state: str | None
    pending_target_role_id: str | None
    pending_handoff_id: str | None


class FlowObligationResponse(ApiModel):
    project_id: str
    work_item_id: str
    state: str
    entry_version: int
    kind: str
    obligation_id: str
    accountable_role_id: str
    payload: dict[str, Any]
    status: str
    evidence: dict[str, Any]


class GovernanceRecordResponse(ApiModel):
    project_id: str
    work_item_id: str
    record_id: str
    state: str
    entry_version: int
    obligation_kind: str
    obligation_id: str
    decision: str
    actor_role_id: str
    reason: str
    evidence: dict[str, Any]
    document_path: str | None
    document_etag: str | None
    recorded_at: str


class FailureIncidentCreate(ApiModel):
    idempotency_key: str = Field(pattern=IDENTIFIER_PATTERN)
    failure_category: str = Field(pattern=IDENTIFIER_PATTERN)
    safe_summary: str = Field(min_length=1, max_length=1000)
    source_ref: str = Field(min_length=1, max_length=1000)


class FailureAttemptCreate(ApiModel):
    attempt_id: str = Field(pattern=IDENTIFIER_PATTERN)
    stage: Literal["technical", "pm_correction", "recovery"]
    attempt_number: int = Field(
        ge=1, le=3, description="Attempt 1 for recovery; attempts 1-3 otherwise."
    )
    outcome: Literal["succeeded", "failed"]
    correction_instruction: str | None = Field(
        default=None,
        max_length=2000,
        description="Required for PM correction and forbidden for other stages.",
    )
    evidence: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_stage_contract(self):
        if self.stage == "recovery" and self.attempt_number != 1:
            raise ValueError("recovery requires attempt_number 1")
        if self.stage == "pm_correction":
            if (
                self.correction_instruction is None
                or not self.correction_instruction.strip()
            ):
                raise ValueError("PM correction requires correction_instruction")
        elif self.correction_instruction is not None:
            raise ValueError(
                "correction_instruction is only valid for PM correction"
            )
        return self


class FailureIncidentResponse(ApiModel):
    project_id: str
    incident_id: str
    work_item_id: str
    owner_role_id: str
    failure_category: str
    safe_summary: str
    source_ref: str
    status: str
    next_stage: str
    next_attempt_number: int | None
    started_by: str
    started_at: str
    resolved_at: str | None


class FailureAttemptResponse(ApiModel):
    project_id: str
    attempt_id: str
    incident_id: str
    ordinal: int
    stage: str
    stage_attempt: int
    outcome: str
    correction_instruction: str | None
    actor_id: str
    evidence: dict[str, Any]
    recorded_at: str


class RecoveryRequestResponse(ApiModel):
    project_id: str
    recovery_request_id: str
    incident_id: str
    work_item_id: str
    exact_goal: str
    status: str
    requested_at: str
    resolved_at: str | None
    evidence: dict[str, Any]


class ReliabilityStatusResponse(ApiModel):
    incident: FailureIncidentResponse
    attempts: list[FailureAttemptResponse]
    recovery_request: RecoveryRequestResponse | None


class RecoveryOverviewResponse(ApiModel):
    project_id: str
    items: list[dict[str, Any]]


class GateOpen(ApiModel):
    gate_id: str = Field(min_length=1)
    gate_type: str = Field(min_length=1)
    sponsor_ids: list[str] = Field(min_length=1)
    correlation_id: str = Field(min_length=1)
    expected_version: int = Field(ge=1)
    flow_obligation_id: str | None = Field(default=None, pattern=IDENTIFIER_PATTERN)
    expires_in_seconds: int = Field(default=172_800, ge=1, le=604_800)
    evidence: dict[str, Any] = Field(default_factory=dict)


class GateDecision(ApiModel):
    decision: Literal["approved", "rejected"]
    rationale: str = Field(min_length=1)
    evidence: dict[str, Any] = Field(default_factory=dict)


class GateResponse(ApiModel):
    project_id: str
    gate_id: str
    work_item_id: str
    gate_type: str
    status: str
    requested_by: str
    correlation_id: str
    evidence: dict[str, Any]
    flow_state: str | None = None
    flow_entry_version: int | None = None
    flow_obligation_id: str | None = None
    expires_at: str | None = None
    timed_out_at: str | None = None


class ApprovalResponse(ApiModel):
    project_id: str
    approval_id: str
    gate_id: str
    approver_id: str
    decision: str | None
    rationale: str | None
    evidence: dict[str, Any]
    decided_at: str | None


class QueueCreate(ApiModel):
    queue_id: str = Field(min_length=1)
    role_id: str = Field(min_length=1)
    capability: str | None = Field(default=None, pattern=IDENTIFIER_PATTERN)


class QueueItemCreate(ApiModel):
    queue_item_id: str = Field(min_length=1)
    work_item_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    priority: int = 0
    available_at: datetime | None = None


class RouteCreate(ApiModel):
    work_item_id: str = Field(pattern=IDENTIFIER_PATTERN)
    target_role_id: str = Field(pattern=IDENTIFIER_PATTERN)
    capability: str | None = Field(default=None, pattern=IDENTIFIER_PATTERN)
    idempotency_key: str = Field(pattern=IDENTIFIER_PATTERN)
    payload: dict[str, Any] = Field(default_factory=dict)
    priority: int = 0
    available_at: datetime | None = None


class QueueClaim(ApiModel):
    owner_instance_id: str = Field(min_length=1)
    lease_seconds: int = Field(ge=1, le=3600)


class LeaseHeartbeat(ApiModel):
    lease_token: str = Field(min_length=1)
    lease_seconds: int = Field(ge=1, le=3600)


class LeaseFinish(ApiModel):
    lease_token: str = Field(min_length=1)


class QueueItemResponse(ApiModel):
    project_id: str
    queue_item_id: str
    queue_id: str
    work_item_id: str
    status: str
    priority: int
    attempt_count: int
    available_at: str
    payload: dict[str, Any]
    idempotency_key: str


class LeaseClaimResponse(ApiModel):
    project_id: str
    lease_id: str
    lease_token: str
    queue_item: QueueItemResponse
    owner_instance_id: str
    acquired_at: str
    heartbeat_at: str
    expires_at: str


class LeaseHeartbeatResponse(ApiModel):
    expires_at: str


class HandoffOfferCreate(ApiModel):
    source_lease_id: str = Field(pattern=IDENTIFIER_PATTERN)
    source_lease_token: str = Field(min_length=1, max_length=512)
    target_role_id: str = Field(pattern=IDENTIFIER_PATTERN)
    capability: str | None = Field(default=None, pattern=IDENTIFIER_PATTERN)
    idempotency_key: str = Field(pattern=IDENTIFIER_PATTERN)
    summary: str = Field(min_length=1, max_length=4000)
    payload: dict[str, Any] = Field(default_factory=dict)
    priority: int = 0


class HandoffLeaseAction(ApiModel):
    lease_id: str = Field(pattern=IDENTIFIER_PATTERN)
    lease_token: str = Field(min_length=1, max_length=512)


class HandoffResponse(ApiModel):
    project_id: str
    handoff_id: str
    work_item_id: str
    source_role_id: str
    source_instance_id: str | None
    source_queue_item_id: str | None
    source_lease_id: str | None
    target_role_id: str
    target_instance_id: str | None
    queue_item_id: str | None
    target_lease_id: str | None
    status: str
    summary: str
    idempotency_key: str
    offered_at: str
    queued_at: str
    claimed_at: str | None
    accepted_at: str | None
    delivery_latency_seconds: float
    claim_latency_seconds: float | None
    acceptance_latency_seconds: float | None
    delivery_target_met: bool
    claim_target_met: bool | None
    acceptance_target_met: bool | None
    claim_overdue: bool
    acceptance_overdue: bool


class PmMonitorClaimCreate(ApiModel):
    owner_id: str = Field(pattern=IDENTIFIER_PATTERN)
    lease_seconds: int = Field(ge=1, le=300)


class PmMonitorAction(ApiModel):
    owner_id: str = Field(pattern=IDENTIFIER_PATTERN)
    lease_token: str = Field(min_length=1, max_length=512)


class PmMonitorClaimResponse(ApiModel):
    logical_pm_id: str
    owner_id: str
    lease_token: str
    acquired_at: str
    heartbeat_at: str
    expires_at: str


class PmMonitorStatusResponse(ApiModel):
    logical_pm_id: str
    owner_id: str
    heartbeat_at: str
    expires_at: str
    active: bool
    sweep_count: int
    last_sweep_at: str | None


class ContinuationObservationResponse(ApiModel):
    project_id: str
    work_item_id: str
    work_version: int
    disposition: str
    action_id: str | None
    detail: str
    observed_at: str


class PmMonitorSweepResponse(ApiModel):
    logical_pm_id: str
    owner_id: str
    sweep_count: int
    observations: list[ContinuationObservationResponse]


class FleetPolicyUpsert(ApiModel):
    min_warm_instances: int = Field(ge=0, le=1000)
    max_instances: int = Field(ge=1, le=1000)
    scale_after_seconds: int = Field(default=60, ge=1, le=3600)
    idle_grace_seconds: int = Field(default=300, ge=1, le=86400)
    hibernation_enabled: bool = True


class FleetPolicyResponse(ApiModel):
    project_id: str
    role_id: str
    min_warm_instances: int
    max_instances: int
    scale_after_seconds: int
    idle_grace_seconds: int
    hibernation_enabled: bool
    updated_at: str | None


class FleetReconcileRequest(ApiModel):
    project_id: str | None = Field(default=None, pattern=IDENTIFIER_PATTERN)


class FleetActionResponse(ApiModel):
    action_id: str
    project_id: str
    role_id: str
    instance_id: str
    action: Literal["wake", "hibernate"]
    reason: str


class FleetActionResultResponse(ApiModel):
    action: FleetActionResponse
    status: Literal["completed", "failed", "superseded"]
    detail: str


class FleetReconcileResponse(ApiModel):
    project_id: str | None
    actions: list[FleetActionResultResponse]


class QueueMetricsResponse(ApiModel):
    project_id: str
    queue_id: str
    depth: int
    ready: int
    delayed: int
    leased: int
    oldest_ready_age_seconds: float | None
    total_attempts: int


class ProgressCreate(ApiModel):
    checkpoint_id: str = Field(min_length=1, max_length=128)
    role_instance_id: str = Field(min_length=1, max_length=128)
    expected_previous_sequence: int = Field(ge=0)
    status: str = Field(min_length=1, max_length=32)
    goal: str = Field(min_length=1, max_length=2000)
    step: str = Field(min_length=1, max_length=2000)
    completed_action: str | None = Field(default=None, max_length=2000)
    activity: str | None = Field(default=None, max_length=2000)
    blocker: str | None = Field(default=None, max_length=2000)
    next_action: str = Field(min_length=1, max_length=2000)
    safe_summary: str = Field(min_length=1, max_length=1000)


class ProgressResponse(ApiModel):
    project_id: str
    progress_id: int
    checkpoint_id: str
    work_item_id: str
    role_instance_id: str
    sequence: int
    status: str
    goal: str
    step: str
    completed_action: str | None
    activity: str | None
    blocker: str | None
    next_action: str
    safe_summary: str
    recorded_at: str


class UsageWindowResponse(ApiModel):
    used_percent: int
    remaining_percent: int
    resets_at: str | None
    window_minutes: int | None


class UsageCreditsResponse(ApiModel):
    balance: str | None
    has_credits: bool
    unlimited: bool


class UsageSpendControlResponse(ApiModel):
    limit: str
    used: str
    remaining_percent: int
    resets_at: str


class UsageCapacityResponse(ApiModel):
    status: Literal["known", "unknown"]
    observed_at: str | None
    limit_id: str | None = None
    limit_name: str | None = None
    plan_type: str | None = None
    primary: UsageWindowResponse | None = None
    secondary: UsageWindowResponse | None = None
    credits: UsageCreditsResponse | None = None
    individual_limit: UsageSpendControlResponse | None = None
    reset_credits_available: int | None = None
    reset_credits_earliest_expiry: str | None = None


class UsageSummaryResponse(ApiModel):
    project_id: str
    provider_id: str
    account_scope: str
    account_scope_shared: bool
    turn_count: int
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int
    total_tokens: int
    average_tokens_per_turn: float | None
    usage_updated_at: str | None
    capacity: UsageCapacityResponse


class RecordsResponse(ApiModel):
    records: list[dict[str, Any]]


class AgentsResponse(ApiModel):
    roles: list[dict[str, Any]]
    instances: list[dict[str, Any]]


class ReadModelSnapshotResponse(ApiModel):
    project_id: str
    last_event_id: int
    latest_event_id: int
    caught_up: bool
    domains: dict[str, list[dict[str, Any]]]


class DashboardPortfolioResponse(ApiModel):
    projects: list[dict[str, Any]]


class DashboardPageResponse(ApiModel):
    project_id: str
    total: int
    limit: int
    offset: int
    items: list[dict[str, Any]]


class DashboardFleetResponse(ApiModel):
    project_id: str
    traffic: dict[str, Any]
    roles: list[dict[str, Any]]
    instances: list[dict[str, Any]]
    queues: list[dict[str, Any]]


class DashboardUsageResponse(ApiModel):
    project_id: str
    traffic: dict[str, Any]
    usage: UsageSummaryResponse


class ConfigDraftCreate(ApiModel):
    draft_id: str = Field(pattern=IDENTIFIER_PATTERN)
    references: list[str] = Field(min_length=1)
    expected_active_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )


class ConfigDraftDecision(ApiModel):
    decision: Literal["approved", "rejected"]
    rationale: str = Field(min_length=1, max_length=4000)


class ConfigDraftActivation(ApiModel):
    reason: str = Field(default="", max_length=4000)


class ConfigRollback(ApiModel):
    target_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(min_length=1, max_length=4000)


class DomainAvailability(ApiModel):
    domain: str
    status: Literal["planned"]
    planned_story: str
    available_operations: list[str] = Field(default_factory=list)


class ControlApiError(RuntimeError):
    def __init__(self, status_code: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.detail = detail


class ControlQueries:
    def __init__(self, database_url: str) -> None:
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise DatabaseConfigurationError("the V5 database URL must use Postgres")
        self._database_url = database_url

    def projects(self, allowed: frozenset[str]) -> list[dict[str, Any]]:
        clause = "" if "*" in allowed else "WHERE p.project_id = ANY(%s)"
        parameters: tuple[object, ...] = () if "*" in allowed else (list(allowed),)
        return self._all(
            f"""
            SELECT p.project_id, p.display_name, p.status,
                   COALESCE(array_agg(s.sponsor_id ORDER BY s.sponsor_id)
                       FILTER (WHERE s.sponsor_id IS NOT NULL), ARRAY[]::text[])
                       AS sponsor_ids
            FROM {SCHEMA}.projects AS p
            LEFT JOIN {SCHEMA}.project_sponsors AS s USING (project_id)
            {clause}
            GROUP BY p.project_id, p.display_name, p.status
            ORDER BY p.project_id
            """,
            parameters,
        )

    def project(self, project_id: str) -> dict[str, Any]:
        records = self._all(
            f"""
            SELECT p.project_id, p.display_name, p.status,
                   COALESCE(array_agg(s.sponsor_id ORDER BY s.sponsor_id)
                       FILTER (WHERE s.sponsor_id IS NOT NULL), ARRAY[]::text[])
                       AS sponsor_ids
            FROM {SCHEMA}.projects AS p
            LEFT JOIN {SCHEMA}.project_sponsors AS s USING (project_id)
            WHERE p.project_id = %s
            GROUP BY p.project_id, p.display_name, p.status
            """,
            (project_id,),
        )
        if not records:
            raise LifecycleNotFound("project not found")
        return records[0]

    def is_sponsor(self, project_id: str, subject: str) -> bool:
        self.project(project_id)
        return bool(
            self._all(
                f"""
                SELECT sponsor_id FROM {SCHEMA}.project_sponsors
                WHERE project_id = %s AND sponsor_id = %s
                """,
                (project_id, subject),
            )
        )

    def agents(self, project_id: str) -> dict[str, list[dict[str, Any]]]:
        self.project(project_id)
        return {
            "roles": self._all(
                f"""
                SELECT role_id, template_id, package_digest, status
                FROM {SCHEMA}.roles WHERE project_id = %s ORDER BY role_id
                """,
                (project_id,),
            ),
            "instances": self._all(
                f"""
                SELECT instance_id, role_id, status, provider_ref,
                       started_at, heartbeat_at, hibernated_at, idle_since,
                       lifecycle_action_id, lifecycle_reason, last_wake_at,
                       last_lifecycle_error
                FROM {SCHEMA}.role_instances
                WHERE project_id = %s ORDER BY instance_id
                """,
                (project_id,),
            ),
        }

    def work_items(self, project_id: str) -> list[dict[str, Any]]:
        self.project(project_id)
        return self._all(
            f"""
            SELECT project_id, work_item_id, title, status,
                   assigned_role_id AS owner_role_id, version, terminal_reason,
                   terminal_evidence
            FROM {SCHEMA}.work_items
            WHERE project_id = %s ORDER BY created_at, work_item_id
            """,
            (project_id,),
        )

    def records(self, project_id: str, domain: str) -> list[dict[str, Any]]:
        self.project(project_id)
        queries = {
            "handoffs": f"""
                SELECT handoff_id, work_item_id, source_role_id, source_instance_id,
                       target_role_id, target_instance_id, status, summary,
                       offered_at, claimed_at, accepted_at
                FROM {SCHEMA}.handoffs
                WHERE project_id = %s ORDER BY offered_at, handoff_id
            """,
            "progress": f"""
                SELECT progress_id, checkpoint_id, work_item_id, role_instance_id,
                       sequence, status, goal, step, completed_action, activity,
                       blocker, next_action, safe_summary, recorded_at
                FROM {SCHEMA}.progress
                WHERE project_id = %s ORDER BY progress_id
            """,
            "configuration": f"""
                SELECT scope, project_id, package_type, package_name, package_version,
                       digest, source_ref, activated_at, created_at
                FROM {SCHEMA}.packages
                WHERE scope = 'organization' OR project_id = %s
                ORDER BY package_type, package_name, package_version
            """,
            "audit": f"""
                SELECT audit_id, actor_id, action, object_type, object_id, details,
                       recorded_at
                FROM {SCHEMA}.audit_records
                WHERE scope = 'project' AND project_id = %s ORDER BY audit_id
            """,
        }
        return self._all(queries[domain], (project_id,))

    def _all(self, query: str, parameters: tuple[object, ...]) -> list[dict[str, Any]]:
        try:
            with psycopg.connect(
                self._database_url, autocommit=True, row_factory=dict_row
            ) as connection:
                return list(connection.execute(query, parameters).fetchall())
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError("control-plane query failed") from exc


def create_app(
    database_url: str,
    *,
    authorizer: Authorizer | None = None,
    telemetry: Telemetry | None = None,
    fleet_supervisor: FleetSupervisor | None = None,
    flow_resolver: Callable[[str], FlowDefinition] | None = None,
    document_store_resolver: Callable[[str], DocumentStore] | None = None,
    config_store_resolver: Callable[[str], ConfigActivationStore] | None = None,
    d8a_proxy: D8AProxy | None = None,
    fleet_reconcile_interval_seconds: float = 5.0,
) -> FastAPI:
    if (
        isinstance(fleet_reconcile_interval_seconds, bool)
        or not isinstance(fleet_reconcile_interval_seconds, (int, float))
        or not math.isfinite(float(fleet_reconcile_interval_seconds))
        or fleet_reconcile_interval_seconds <= 0
        or fleet_reconcile_interval_seconds > 300
    ):
        raise ValueError("fleet reconciliation interval is invalid")
    selected_authorizer = authorizer or authorizer_from_environment()
    selected_telemetry = telemetry or Telemetry()
    lifecycle = LifecycleStore(database_url)
    sponsor_approvals = SponsorApprovalCoordinator(database_url)
    progress_store = ProgressStore(database_url)
    usage_store = UsageStore(database_url)
    queues = RoleQueueStore(database_url)
    router = Router(database_url)
    handoff_store = HandoffStore(database_url)
    flow_engine = FlowEngine(database_url)
    continuation_monitor = ContinuationMonitor(database_url)
    fleet_scaler = FleetScaler(database_url, fleet_supervisor)
    reliability = ReliabilityStore(database_url)
    recovery_supervisor = RecoverySupervisorStore(database_url)
    queries = ControlQueries(database_url)
    read_models = ReadModelStore(database_url)
    dashboard_reads = DashboardReadStore(database_url)
    health_reporter = HealthReporter(database_url, selected_telemetry)
    selected_d8a_proxy = d8a_proxy
    d8a_base_url = os.environ.get("AGENTIC_MESH_V5_D8A_BASE_URL", "").strip()
    d8a_tenant_slug = os.environ.get(
        "AGENTIC_MESH_V5_D8A_TENANT_SLUG", ""
    ).strip()
    if selected_d8a_proxy is None and d8a_base_url:
        if not d8a_tenant_slug:
            raise ValueError(
                "AGENTIC_MESH_V5_D8A_TENANT_SLUG is required when "
                "AGENTIC_MESH_V5_D8A_BASE_URL is configured"
            )
        selected_d8a_proxy = D8AProxy(
            manifest_store=ProjectManifestStore(database_url),
            base_url=d8a_base_url,
            tenant_slug=d8a_tenant_slug,
            tenant_id=os.environ.get("AGENTIC_MESH_V5_D8A_TENANT_ID", "").strip()
            or None,
        )
    bearer = HTTPBearer(auto_error=False)

    def project_flow(project_id: str) -> FlowDefinition:
        if flow_resolver is None:
            raise ControlApiError(
                503,
                "flow_configuration_unavailable",
                "project flow configuration is unavailable",
            )
        try:
            flow = flow_resolver(project_id)
        except FlowDefinitionError:
            raise ControlApiError(
                503,
                "flow_configuration_unavailable",
                "project flow configuration is unavailable",
            ) from None
        if not isinstance(flow, FlowDefinition):
            raise ControlApiError(
                503,
                "flow_configuration_unavailable",
                "project flow configuration is invalid",
            )
        return flow

    def governance(project_id: str) -> GovernanceStore:
        if document_store_resolver is None:
            raise ControlApiError(
                503,
                "document_store_unavailable",
                "project document store is unavailable",
            )
        documents = document_store_resolver(project_id)
        if not isinstance(documents, DocumentStore):
            raise ControlApiError(
                503,
                "document_store_unavailable",
                "project document store is invalid",
            )
        return GovernanceStore(database_url, documents)

    def configuration_promotions(project_id: str) -> ConfigPromotionStore:
        queries.project(project_id)
        if config_store_resolver is None:
            raise ControlApiError(
                503,
                "configuration_repository_unavailable",
                "external configuration repository is unavailable",
            )
        try:
            store = config_store_resolver(project_id)
        except ConfigActivationError as exc:
            raise ControlApiError(
                503,
                "configuration_repository_unavailable",
                "external configuration repository is unavailable",
            ) from exc
        if not isinstance(store, ConfigActivationStore):
            raise ControlApiError(
                503,
                "configuration_repository_unavailable",
                "external configuration repository is invalid",
            )
        return ConfigPromotionStore(store, project_id=project_id)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        async def reconcile_fleet() -> None:
            while True:
                with selected_telemetry.operation("fleet.reconcile") as span:
                    try:
                        result = await anyio.to_thread.run_sync(
                            fleet_scaler.reconcile
                        )
                        if any(item.status == "failed" for item in result.actions):
                            selected_telemetry.record_safe_failure(
                                span, code="fleet.supervisor_failed"
                            )
                    except (DatabaseError, FleetSupervisorError, psycopg.Error):
                        selected_telemetry.record_safe_failure(
                            span, code="fleet.reconciliation_failed"
                        )
                await anyio.sleep(float(fleet_reconcile_interval_seconds))

        try:
            if fleet_supervisor is None:
                yield
            else:
                async with anyio.create_task_group() as tasks:
                    tasks.start_soon(reconcile_fleet)
                    try:
                        yield
                    finally:
                        tasks.cancel_scope.cancel()
        finally:
            if isinstance(selected_d8a_proxy, D8AProxy):
                await selected_d8a_proxy.close()
            selected_telemetry.shutdown()

    app = FastAPI(
        title="Agentic Mesh V5 Control API",
        version=__version__,
        description=(
            "Versioned control boundary for the durable Agentic Mesh V5 kernel. "
            "All project operations require an externally configured bearer identity."
        ),
        docs_url="/docs",
        openapi_url="/openapi.json",
        lifespan=lifespan,
        responses={
            code: {
                "model": Problem,
                "description": description,
                "content": {"application/problem+json": {"schema": {"$ref": "#/components/schemas/Problem"}}},
            }
            for code, description in {
                401: "Authentication required or invalid",
                403: "Operation or project access denied",
                404: "Project resource not found",
                409: "Concurrent or lifecycle conflict",
                422: "Request validation failed",
                500: "Unexpected internal failure", 503: "Durable store unavailable",
            }.items()
        },
    )

    @app.middleware("http")
    async def request_identity(request: Request, call_next):
        supplied = request.headers.get("x-request-id", "")
        request_id = (
            supplied
            if REQUEST_ID_PATTERN.fullmatch(supplied)
            else uuid.uuid4().hex
        )
        request.state.request_id = request_id
        method = request.method.upper()
        with selected_telemetry.request(
            method=method,
            request_id=request_id,
            carrier=request.headers,
        ) as observation:
            try:
                response = await call_next(request)
            except Exception:
                selected_telemetry.finish_request(
                    observation,
                    method=method,
                    route=_request_route(request),
                    status_code=500,
                    identifiers=request.path_params,
                )
                raise
            selected_telemetry.finish_request(
                observation,
                method=method,
                route=_request_route(request),
                status_code=response.status_code,
                identifiers=request.path_params,
            )
            selected_telemetry.inject_response_context(response.headers)
        response.headers["x-request-id"] = request_id
        return response

    async def principal(
        credentials: HTTPAuthorizationCredentials | None = Security(bearer),
    ) -> Principal:
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise ControlApiError(401, "authentication_required", "bearer token required")
        resolved = selected_authorizer.resolve(credentials.credentials)
        if resolved is None:
            raise ControlApiError(401, "authentication_invalid", "bearer token is invalid")
        return resolved

    def project_access(identity: Principal, project_id: str, scope: str) -> None:
        if not identity.permits(project_id=project_id, scope=scope):
            raise ControlApiError(
                403,
                "project_access_denied",
                f"identity is not authorized to {scope} project {project_id}",
            )

    def scope_access(identity: Principal, scope: str) -> None:
        if scope not in identity.scopes:
            raise ControlApiError(
                403, "scope_denied", f"identity does not have the {scope} scope"
            )

    def organization_access(identity: Principal, scope: str) -> None:
        scope_access(identity, scope)
        if "*" not in identity.projects:
            raise ControlApiError(
                403, "organization_access_denied",
                "identity is not authorized for organization-wide operation",
            )

    async def forward_d8aroom_documents(
        project_id: str,
        path: str,
        request: Request,
        identity: Principal,
    ) -> Response:
        scope = "read" if request.method == "GET" else "write"
        project_access(identity, project_id, scope)
        if selected_d8a_proxy is None:
            raise ControlApiError(
                503, "d8a_unavailable", "D8Aroom document proxy is not configured"
            )
        query = {key: value for key, value in request.query_params.multi_items()}
        try:
            result = await selected_d8a_proxy.forward(
                project_id=project_id,
                method=request.method,
                path=path,
                query=query,
                content=await request.body(),
                content_type=request.headers.get("content-type"),
                principal=identity,
                request_id=request.state.request_id,
            )
        except D8AProxyError as exc:
            raise ControlApiError(exc.status_code, exc.code, exc.detail) from exc
        if result.stream is not None:
            return StreamingResponse(
                result.stream,
                status_code=result.status_code,
                headers=dict(result.headers),
                background=(
                    BackgroundTask(result.close) if result.close is not None else None
                ),
            )
        return Response(
            content=result.content,
            status_code=result.status_code,
            headers=dict(result.headers),
        )

    _d8a_path = f"{API_PREFIX}/projects/{{project_id}}/documents/d8aroom/{{path:path}}"

    @app.get(_d8a_path, response_class=Response)
    async def get_d8aroom_documents(
        project_id: str,
        path: str,
        request: Request,
        identity: Principal = Depends(principal),
    ) -> Response:
        return await forward_d8aroom_documents(
            project_id, path, request, identity
        )

    @app.post(_d8a_path, response_class=Response)
    async def post_d8aroom_documents(
        project_id: str,
        path: str,
        request: Request,
        identity: Principal = Depends(principal),
    ) -> Response:
        return await forward_d8aroom_documents(
            project_id, path, request, identity
        )

    @app.put(_d8a_path, response_class=Response)
    async def put_d8aroom_documents(
        project_id: str,
        path: str,
        request: Request,
        identity: Principal = Depends(principal),
    ) -> Response:
        return await forward_d8aroom_documents(
            project_id, path, request, identity
        )

    @app.exception_handler(ControlApiError)
    async def control_error(request: Request, exc: ControlApiError) -> JSONResponse:
        return _problem_response(
            request, exc.status_code, exc.code, exc.detail,
            authenticate=exc.status_code == 401,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        errors = [
            {"location": list(item["loc"]), "message": item["msg"], "type": item["type"]}
            for item in exc.errors()
        ]
        return _problem_response(
            request, 422, "validation_failed", "request validation failed", errors=errors
        )

    @app.exception_handler(LifecycleNotFound)
    @app.exception_handler(QueueNotFound)
    @app.exception_handler(ReadModelNotFound)
    @app.exception_handler(ProgressNotFound)
    @app.exception_handler(UsageNotFound)
    @app.exception_handler(RoutingNotFound)
    @app.exception_handler(HandoffNotFound)
    @app.exception_handler(FleetNotFound)
    @app.exception_handler(ReliabilityNotFound)
    @app.exception_handler(FlowEngineNotFound)
    @app.exception_handler(GovernanceNotFound)
    @app.exception_handler(DocumentNotFound)
    @app.exception_handler(ConfigPromotionNotFound)
    async def not_found(request: Request, exc: Exception) -> JSONResponse:
        return _problem_response(request, 404, "not_found", str(exc))

    @app.exception_handler(LifecycleConflict)
    @app.exception_handler(QueueConflict)
    @app.exception_handler(LeaseExpired)
    @app.exception_handler(ProgressConflict)
    @app.exception_handler(RoutingConflict)
    @app.exception_handler(HandoffConflict)
    @app.exception_handler(ContinuationConflict)
    @app.exception_handler(FleetConflict)
    @app.exception_handler(ReliabilityConflict)
    @app.exception_handler(FlowEngineConflict)
    @app.exception_handler(GovernanceConflict)
    @app.exception_handler(DocumentConflict)
    @app.exception_handler(ConfigPromotionConflict)
    @app.exception_handler(ConfigPromotionError)
    @app.exception_handler(ConfigActivationError)
    async def conflict(request: Request, exc: Exception) -> JSONResponse:
        return _problem_response(request, 409, "conflict", str(exc))

    @app.exception_handler(LifecycleAuthorizationError)
    @app.exception_handler(QueueAuthorizationError)
    @app.exception_handler(HandoffAuthorizationError)
    @app.exception_handler(ContinuationAuthorizationError)
    @app.exception_handler(DocumentPermissionDenied)
    async def forbidden(request: Request, exc: Exception) -> JSONResponse:
        return _problem_response(request, 403, "operation_forbidden", str(exc))

    @app.exception_handler(DocumentUnavailable)
    async def document_unavailable(request: Request, exc: Exception) -> JSONResponse:
        return _problem_response(request, 503, "document_store_unavailable", str(exc))

    @app.exception_handler(ValueError)
    async def invalid_value(request: Request, exc: ValueError) -> JSONResponse:
        return _problem_response(request, 422, "validation_failed", str(exc))

    @app.exception_handler(FleetSupervisorError)
    async def fleet_unavailable(
        request: Request, _exc: FleetSupervisorError
    ) -> JSONResponse:
        return _problem_response(
            request, 503, "fleet_supervisor_unavailable",
            "fleet supervisor is unavailable",
        )

    @app.exception_handler(DatabaseError)
    @app.exception_handler(psycopg.Error)
    async def service_failure(request: Request, _exc: DatabaseError | psycopg.Error) -> JSONResponse:
        return _problem_response(
            request, 503, "durable_store_unavailable", "durable store operation failed"
        )

    @app.exception_handler(Exception)
    async def unexpected_failure(request: Request, _exc: Exception) -> JSONResponse:
        return _problem_response(request, 500, "internal_error", "unexpected control-plane failure")

    def readiness(response: Response) -> dict[str, object]:
        report = health_reporter.readiness()
        if report.status == "degraded":
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return report.to_dict()

    @app.get(f"{API_PREFIX}/health/live", response_model=HealthResponse, tags=["system"])
    def health_live() -> dict[str, object]:
        return health_reporter.liveness().to_dict()

    @app.get(f"{API_PREFIX}/health/ready", response_model=HealthResponse, tags=["system"])
    def health_ready(response: Response) -> dict[str, object]:
        return readiness(response)

    @app.get(f"{API_PREFIX}/health", response_model=HealthResponse, tags=["system"])
    def health(response: Response) -> dict[str, object]:
        return readiness(response)

    @app.post(
        f"{API_PREFIX}/pm-monitor/claim",
        response_model=PmMonitorClaimResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["pm-monitor"],
    )
    def claim_pm_monitor(
        payload: PmMonitorClaimCreate,
        identity: Principal = Depends(principal),
    ):
        organization_access(identity, "write")
        return asdict(
            continuation_monitor.claim(
                owner_id=payload.owner_id, lease_seconds=payload.lease_seconds
            )
        )

    @app.post(
        f"{API_PREFIX}/pm-monitor/heartbeat",
        response_model=PmMonitorStatusResponse,
        tags=["pm-monitor"],
    )
    def heartbeat_pm_monitor(
        payload: PmMonitorAction,
        identity: Principal = Depends(principal),
    ):
        organization_access(identity, "write")
        return asdict(
            continuation_monitor.heartbeat(
                owner_id=payload.owner_id, lease_token=payload.lease_token
            )
        )

    @app.post(
        f"{API_PREFIX}/pm-monitor/sweep",
        response_model=PmMonitorSweepResponse,
        tags=["pm-monitor"],
    )
    def sweep_pm_monitor(
        payload: PmMonitorAction,
        identity: Principal = Depends(principal),
    ):
        organization_access(identity, "write")
        return asdict(
            continuation_monitor.sweep(
                owner_id=payload.owner_id, lease_token=payload.lease_token
            )
        )

    @app.get(
        f"{API_PREFIX}/pm-monitor",
        response_model=PmMonitorStatusResponse,
        tags=["pm-monitor"],
    )
    def get_pm_monitor(identity: Principal = Depends(principal)):
        organization_access(identity, "read")
        return asdict(continuation_monitor.status())

    @app.put(
        f"{API_PREFIX}/projects/{{project_id}}/fleet/policies/{{role_id}}",
        response_model=FleetPolicyResponse,
        tags=["fleet"],
    )
    def configure_fleet_policy(
        project_id: str,
        role_id: str,
        payload: FleetPolicyUpsert,
        identity: Principal = Depends(principal),
    ):
        project_access(identity, project_id, "write")
        return asdict(
            fleet_scaler.configure(
                ScalingPolicy(project_id=project_id, role_id=role_id, **payload.model_dump())
            )
        )

    @app.get(
        f"{API_PREFIX}/projects/{{project_id}}/fleet/policies",
        response_model=list[FleetPolicyResponse],
        tags=["fleet"],
    )
    def fleet_policies(
        project_id: str, identity: Principal = Depends(principal)
    ):
        project_access(identity, project_id, "read")
        return [asdict(item) for item in fleet_scaler.policies(project_id)]

    @app.post(
        f"{API_PREFIX}/fleet/reconcile",
        response_model=FleetReconcileResponse,
        tags=["fleet"],
    )
    def reconcile_fleet(
        payload: FleetReconcileRequest,
        identity: Principal = Depends(principal),
    ):
        organization_access(identity, "write")
        return asdict(fleet_scaler.reconcile(payload.project_id))

    @app.get(f"{API_PREFIX}/projects", response_model=list[ProjectResponse], tags=["projects"])
    def list_projects(identity: Principal = Depends(principal)):
        scope_access(identity, "read")
        return queries.projects(identity.projects)

    @app.post(
        f"{API_PREFIX}/projects",
        response_model=ProjectResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["projects"],
    )
    def create_project(payload: ProjectCreate, identity: Principal = Depends(principal)):
        project_access(identity, payload.project_id, "project:create")
        lifecycle.create_project(
            project_id=payload.project_id,
            display_name=payload.display_name,
            sponsor_ids=payload.sponsor_ids,
        )
        return queries.project(payload.project_id)

    @app.get(
        f"{API_PREFIX}/projects/{{project_id}}",
        response_model=ProjectResponse,
        tags=["projects"],
    )
    def get_project(project_id: str, identity: Principal = Depends(principal)):
        project_access(identity, project_id, "read")
        return queries.project(project_id)

    @app.post(
        f"{API_PREFIX}/projects/{{project_id}}/work-items",
        response_model=WorkItemResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["work"],
    )
    def create_work_item(
        project_id: str,
        payload: WorkItemCreate,
        identity: Principal = Depends(principal),
    ):
        project_access(identity, project_id, "write")
        selected_telemetry.annotate(
            project_id=project_id,
            work_item_id=payload.work_item_id,
            correlation_id=payload.correlation_id,
        )
        return asdict(
            lifecycle.create_work_item(
                project_id=project_id,
                work_item_id=payload.work_item_id,
                title=payload.title,
                owner_role_id=payload.owner_role_id,
                actor_id=identity.subject,
                correlation_id=payload.correlation_id,
            )
        )

    @app.get(
        f"{API_PREFIX}/projects/{{project_id}}/work-items",
        response_model=list[WorkItemResponse],
        tags=["work"],
    )
    def list_work_items(project_id: str, identity: Principal = Depends(principal)):
        project_access(identity, project_id, "read")
        return queries.work_items(project_id)

    @app.get(
        f"{API_PREFIX}/projects/{{project_id}}/work-items/{{work_item_id}}",
        response_model=WorkItemResponse,
        tags=["work"],
    )
    def get_work_item(
        project_id: str,
        work_item_id: str,
        identity: Principal = Depends(principal),
    ):
        project_access(identity, project_id, "read")
        return asdict(lifecycle.get_work_item(project_id, work_item_id))

    @app.post(
        f"{API_PREFIX}/projects/{{project_id}}/work-items/{{work_item_id}}/transitions",
        response_model=WorkItemResponse,
        tags=["work"],
    )
    def transition_work_item(
        project_id: str,
        work_item_id: str,
        payload: WorkItemTransition,
        identity: Principal = Depends(principal),
    ):
        project_access(identity, project_id, "write")
        selected_telemetry.annotate(
            project_id=project_id,
            work_item_id=work_item_id,
            correlation_id=payload.correlation_id,
        )
        return asdict(
            lifecycle.transition_work_item(
                project_id=project_id,
                work_item_id=work_item_id,
                target_status=payload.target_status,
                actor_id=identity.subject,
                correlation_id=payload.correlation_id,
                expected_version=payload.expected_version,
                reason=payload.reason,
                evidence=payload.evidence,
            )
        )

    def require_flow_role(project_id: str, work_item_id: str, role_id: str) -> None:
        run = flow_engine.get(project_id, work_item_id)
        if run.owner_role_id != role_id:
            raise ControlApiError(
                403,
                "flow_role_denied",
                "authenticated role does not own the current flow state",
            )

    @app.post(
        f"{API_PREFIX}/projects/{{project_id}}/work-items/{{work_item_id}}/flow",
        response_model=FlowRunResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["flow"],
    )
    def start_flow(
        project_id: str,
        work_item_id: str,
        payload: FlowStart,
        identity: Principal = Depends(principal),
    ):
        project_access(identity, project_id, "write")
        flow = project_flow(project_id)
        if identity.subject != flow.leader_role:
            raise ControlApiError(
                403,
                "flow_role_denied",
                "only the configured flow leader can start a flow",
            )
        return asdict(
            flow_engine.start(
                project_id=project_id,
                work_item_id=work_item_id,
                flow=flow,
                fields=payload.fields,
                actor_id=identity.subject,
                operation_id=payload.operation_id,
            )
        )

    @app.get(
        f"{API_PREFIX}/projects/{{project_id}}/work-items/{{work_item_id}}/flow",
        response_model=FlowRunResponse,
        tags=["flow"],
    )
    def get_flow(
        project_id: str,
        work_item_id: str,
        identity: Principal = Depends(principal),
    ):
        project_access(identity, project_id, "read")
        return asdict(flow_engine.get(project_id, work_item_id))

    @app.get(
        f"{API_PREFIX}/projects/{{project_id}}/work-items/{{work_item_id}}/"
        "flow/obligations",
        response_model=list[FlowObligationResponse],
        tags=["flow"],
    )
    def flow_obligations(
        project_id: str,
        work_item_id: str,
        current_only: bool = Query(default=True),
        identity: Principal = Depends(principal),
    ):
        project_access(identity, project_id, "read")
        return [
            asdict(item)
            for item in flow_engine.obligations(
                project_id, work_item_id, current_only=current_only
            )
        ]

    @app.post(
        f"{API_PREFIX}/projects/{{project_id}}/work-items/{{work_item_id}}/"
        "flow/dispatch",
        response_model=FlowObligationResponse,
        tags=["flow"],
    )
    def dispatch_flow_obligation(
        project_id: str,
        work_item_id: str,
        payload: FlowDispatch,
        identity: Principal = Depends(principal),
    ):
        project_access(identity, project_id, "write")
        require_flow_role(project_id, work_item_id, identity.subject)
        return asdict(
            flow_engine.dispatch(
                project_id=project_id,
                work_item_id=work_item_id,
                kind=payload.kind,
                obligation_id=payload.obligation_id,
                actor_id=identity.subject,
            )
        )

    @app.post(
        f"{API_PREFIX}/projects/{{project_id}}/work-items/{{work_item_id}}/"
        "flow/artifact",
        response_model=GovernanceRecordResponse,
        tags=["governance"],
    )
    def verify_flow_artifact(
        project_id: str,
        work_item_id: str,
        payload: ArtifactVerify,
        identity: Principal = Depends(principal),
    ):
        project_access(identity, project_id, "write")
        return asdict(
            governance(project_id).verify_artifact(
                project_id=project_id,
                work_item_id=work_item_id,
                path=payload.path,
                actor_role_id=identity.subject,
                record_id=payload.record_id,
            )
        )

    @app.post(
        f"{API_PREFIX}/projects/{{project_id}}/work-items/{{work_item_id}}/"
        "flow/consultations",
        response_model=GovernanceRecordResponse,
        tags=["governance"],
    )
    def record_flow_consultation(
        project_id: str,
        work_item_id: str,
        payload: ConsultationRecord,
        identity: Principal = Depends(principal),
    ):
        project_access(identity, project_id, "write")
        return asdict(
            governance(project_id).record_consultation(
                project_id=project_id,
                work_item_id=work_item_id,
                obligation_id=payload.obligation_id,
                decision=payload.decision,
                actor_role_id=identity.subject,
                record_id=payload.record_id,
                reason=payload.reason,
                evidence=payload.evidence,
            )
        )

    @app.post(
        f"{API_PREFIX}/projects/{{project_id}}/work-items/{{work_item_id}}/"
        "flow/gates",
        response_model=GovernanceRecordResponse,
        tags=["governance"],
    )
    def decide_flow_gate(
        project_id: str,
        work_item_id: str,
        payload: GovernanceGateDecision,
        identity: Principal = Depends(principal),
    ):
        project_access(identity, project_id, "write")
        return asdict(
            governance(project_id).decide_gate(
                project_id=project_id,
                work_item_id=work_item_id,
                obligation_id=payload.obligation_id,
                decision=payload.decision,
                actor_role_id=identity.subject,
                record_id=payload.record_id,
                reason=payload.reason,
                evidence=payload.evidence,
                architecture_impact=payload.architecture_impact,
            )
        )

    @app.get(
        f"{API_PREFIX}/projects/{{project_id}}/work-items/{{work_item_id}}/"
        "flow/governance",
        response_model=list[GovernanceRecordResponse],
        tags=["governance"],
    )
    def flow_governance_records(
        project_id: str,
        work_item_id: str,
        identity: Principal = Depends(principal),
    ):
        project_access(identity, project_id, "read")
        return [
            asdict(item)
            for item in governance(project_id).records(project_id, work_item_id)
        ]

    @app.post(
        f"{API_PREFIX}/projects/{{project_id}}/work-items/{{work_item_id}}/"
        "flow/transitions/prepare",
        response_model=FlowRunResponse,
        tags=["flow"],
    )
    def prepare_flow_transition(
        project_id: str,
        work_item_id: str,
        payload: FlowTransitionPrepare,
        identity: Principal = Depends(principal),
    ):
        project_access(identity, project_id, "write")
        require_flow_role(project_id, work_item_id, identity.subject)
        return asdict(
            flow_engine.prepare_transition(
                project_id=project_id,
                work_item_id=work_item_id,
                outcome=payload.outcome,
                fields=payload.fields,
                source=TransitionSource(
                    payload.source_lease_id, payload.source_lease_token
                ),
                expected_version=payload.expected_version,
                actor_id=identity.subject,
                operation_id=payload.operation_id,
            )
        )

    @app.post(
        f"{API_PREFIX}/projects/{{project_id}}/work-items/{{work_item_id}}/"
        "flow/transitions/pickup",
        response_model=FlowRunResponse,
        tags=["flow"],
    )
    def pickup_flow_transition(
        project_id: str,
        work_item_id: str,
        payload: FlowTransitionAction,
        identity: Principal = Depends(principal),
    ):
        project_access(identity, project_id, "write")
        run = flow_engine.get(project_id, work_item_id)
        if identity.subject != run.pending_target_role_id:
            raise ControlApiError(
                403,
                "flow_role_denied",
                "only the accepted target role can pick up a transition",
            )
        return asdict(
            flow_engine.pickup_transition(
                project_id=project_id,
                work_item_id=work_item_id,
                expected_version=payload.expected_version,
                actor_id=identity.subject,
                operation_id=payload.operation_id,
            )
        )

    @app.post(
        f"{API_PREFIX}/projects/{{project_id}}/work-items/{{work_item_id}}/"
        "flow/complete",
        response_model=FlowRunResponse,
        tags=["flow"],
    )
    def complete_flow(
        project_id: str,
        work_item_id: str,
        payload: FlowComplete,
        identity: Principal = Depends(principal),
    ):
        project_access(identity, project_id, "write")
        require_flow_role(project_id, work_item_id, identity.subject)
        return asdict(
            flow_engine.complete(
                project_id=project_id,
                work_item_id=work_item_id,
                expected_version=payload.expected_version,
                actor_id=identity.subject,
                operation_id=payload.operation_id,
                evidence=payload.evidence,
            )
        )

    @app.post(
        f"{API_PREFIX}/projects/{{project_id}}/work-items/{{work_item_id}}/reliability/incidents",
        response_model=ReliabilityStatusResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["reliability"],
    )
    def start_failure_incident(
        project_id: str,
        work_item_id: str,
        payload: FailureIncidentCreate,
        identity: Principal = Depends(principal),
    ):
        project_access(identity, project_id, "write")
        selected_telemetry.annotate(
            project_id=project_id, work_item_id=work_item_id
        )
        return asdict(
            reliability.start(
                project_id=project_id,
                work_item_id=work_item_id,
                idempotency_key=payload.idempotency_key,
                failure_category=payload.failure_category,
                safe_summary=payload.safe_summary,
                source_ref=payload.source_ref,
                actor_id=identity.subject,
            )
        )

    @app.post(
        f"{API_PREFIX}/projects/{{project_id}}/work-items/{{work_item_id}}/reliability/incidents/{{incident_id}}/attempts",
        response_model=ReliabilityStatusResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["reliability"],
    )
    def record_failure_attempt(
        project_id: str,
        work_item_id: str,
        incident_id: str,
        payload: FailureAttemptCreate,
        identity: Principal = Depends(principal),
    ):
        project_access(identity, project_id, "write")
        if payload.stage == "recovery":
            raise ControlApiError(
                403,
                "independent_recovery_required",
                "recovery results are accepted only from the independent supervisor",
            )
        selected_telemetry.annotate(
            project_id=project_id, work_item_id=work_item_id
        )
        return asdict(
            reliability.record_attempt(
                project_id=project_id,
                work_item_id=work_item_id,
                incident_id=incident_id,
                attempt_id=payload.attempt_id,
                stage=payload.stage,
                attempt_number=payload.attempt_number,
                outcome=payload.outcome,
                correction_instruction=payload.correction_instruction,
                evidence=payload.evidence,
                actor_id=identity.subject,
            )
        )

    @app.get(
        f"{API_PREFIX}/projects/{{project_id}}/work-items/{{work_item_id}}/reliability",
        response_model=ReliabilityStatusResponse,
        tags=["reliability"],
    )
    def failure_status(
        project_id: str,
        work_item_id: str,
        incident_id: str | None = Query(default=None, pattern=IDENTIFIER_PATTERN),
        identity: Principal = Depends(principal),
    ):
        project_access(identity, project_id, "read")
        return asdict(reliability.status(project_id, work_item_id, incident_id))

    @app.post(
        f"{API_PREFIX}/projects/{{project_id}}/work-items/{{work_item_id}}/gates",
        response_model=GateResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["gates"],
    )
    def open_gate(
        project_id: str,
        work_item_id: str,
        payload: GateOpen,
        identity: Principal = Depends(principal),
    ):
        project_access(identity, project_id, "write")
        selected_telemetry.annotate(
            project_id=project_id,
            work_item_id=work_item_id,
            correlation_id=payload.correlation_id,
        )
        if payload.gate_type in {"sponsor_approval", "human_response"}:
            return asdict(
                sponsor_approvals.open(
                    project_id=project_id,
                    work_item_id=work_item_id,
                    gate_id=payload.gate_id,
                    obligation_id=payload.flow_obligation_id or payload.gate_id,
                    requested_by=identity.subject,
                    sponsor_ids=payload.sponsor_ids,
                    correlation_id=payload.correlation_id,
                    expected_version=payload.expected_version,
                    expires_in_seconds=payload.expires_in_seconds,
                    evidence=payload.evidence,
                )
            )
        if payload.flow_obligation_id is not None:
            raise ValueError(
                "flow_obligation_id is valid only for sponsor approval gates"
            )
        return asdict(
            lifecycle.open_gate(
                project_id=project_id,
                work_item_id=work_item_id,
                gate_id=payload.gate_id,
                gate_type=payload.gate_type,
                requested_by=identity.subject,
                sponsor_ids=payload.sponsor_ids,
                correlation_id=payload.correlation_id,
                expected_version=payload.expected_version,
                evidence=payload.evidence,
            )
        )

    @app.get(
        f"{API_PREFIX}/projects/{{project_id}}/gates/{{gate_id}}",
        response_model=GateResponse,
        tags=["gates"],
    )
    def get_gate(project_id: str, gate_id: str, identity: Principal = Depends(principal)):
        project_access(identity, project_id, "read")
        if sponsor_approvals.is_managed(project_id, gate_id):
            return asdict(sponsor_approvals.get(project_id, gate_id))
        return asdict(lifecycle.get_gate(project_id, gate_id))

    @app.post(
        f"{API_PREFIX}/projects/{{project_id}}/gates/{{gate_id}}/decision",
        response_model=ApprovalResponse,
        tags=["approvals"],
    )
    def decide_gate(
        project_id: str,
        gate_id: str,
        payload: GateDecision,
        request: Request,
        identity: Principal = Depends(principal),
    ):
        project_access(identity, project_id, "write")
        if sponsor_approvals.is_managed(project_id, gate_id):
            return asdict(
                sponsor_approvals.decide(
                    project_id=project_id,
                    gate_id=gate_id,
                    sponsor_id=identity.subject,
                    decision=payload.decision,
                    rationale=payload.rationale,
                    evidence=payload.evidence,
                    operation_id=request.state.request_id,
                )
            )
        return asdict(
            lifecycle.decide_gate(
                project_id=project_id,
                gate_id=gate_id,
                sponsor_id=identity.subject,
                decision=payload.decision,
                rationale=payload.rationale,
                evidence=payload.evidence,
            )
        )

    @app.post(
        f"{API_PREFIX}/projects/{{project_id}}/gates/{{gate_id}}/timeout",
        response_model=GateResponse,
        tags=["approvals"],
    )
    def timeout_gate(
        project_id: str,
        gate_id: str,
        request: Request,
        identity: Principal = Depends(principal),
    ):
        project_access(identity, project_id, "write")
        return asdict(
            sponsor_approvals.timeout(
                project_id=project_id,
                gate_id=gate_id,
                actor_id=identity.subject,
                operation_id=request.state.request_id,
            )
        )

    @app.get(
        f"{API_PREFIX}/projects/{{project_id}}/approvals/{{approval_id}}",
        response_model=ApprovalResponse,
        tags=["approvals"],
    )
    def get_approval(
        project_id: str,
        approval_id: str,
        identity: Principal = Depends(principal),
    ):
        project_access(identity, project_id, "read")
        return asdict(lifecycle.get_approval(project_id, approval_id))

    @app.post(
        f"{API_PREFIX}/projects/{{project_id}}/queues",
        status_code=status.HTTP_201_CREATED,
        tags=["queues"],
    )
    def create_queue(
        project_id: str,
        payload: QueueCreate,
        identity: Principal = Depends(principal),
    ) -> dict[str, str]:
        project_access(identity, project_id, "write")
        queues.create_queue(
            project_id=project_id,
            queue_id=payload.queue_id,
            role_id=payload.role_id,
            capability=payload.capability,
        )
        return {"project_id": project_id, "queue_id": payload.queue_id}

    @app.post(
        f"{API_PREFIX}/projects/{{project_id}}/handoffs",
        response_model=HandoffResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["handoffs"],
    )
    def offer_handoff(
        project_id: str,
        payload: HandoffOfferCreate,
        identity: Principal = Depends(principal),
    ):
        project_access(identity, project_id, "write")
        offered = handoff_store.offer(
            HandoffOffer(
                project_id=project_id,
                source_lease_id=payload.source_lease_id,
                source_lease_token=payload.source_lease_token,
                target_role_id=payload.target_role_id,
                capability=payload.capability,
                idempotency_key=payload.idempotency_key,
                summary=payload.summary,
                payload=payload.payload,
                priority=payload.priority,
            )
        )
        selected_telemetry.annotate(
            project_id=project_id,
            work_item_id=offered.work_item_id,
            handoff_id=offered.handoff_id,
        )
        return asdict(offered)

    @app.get(
        f"{API_PREFIX}/projects/{{project_id}}/handoffs/{{handoff_id}}",
        response_model=HandoffResponse,
        tags=["handoffs"],
    )
    def get_handoff(
        project_id: str,
        handoff_id: str,
        identity: Principal = Depends(principal),
    ):
        project_access(identity, project_id, "read")
        return asdict(handoff_store.get(project_id, handoff_id))

    def transition_handoff(
        action: Literal["claim", "accept"],
        project_id: str,
        handoff_id: str,
        payload: HandoffLeaseAction,
        identity: Principal,
    ):
        project_access(identity, project_id, "write")
        operation = (
            handoff_store.claim if action == "claim" else handoff_store.accept
        )
        transitioned = operation(
            project_id=project_id,
            handoff_id=handoff_id,
            lease_id=payload.lease_id,
            lease_token=payload.lease_token,
        )
        selected_telemetry.annotate(
            project_id=project_id,
            work_item_id=transitioned.work_item_id,
            handoff_id=handoff_id,
        )
        return asdict(transitioned)

    @app.post(
        f"{API_PREFIX}/projects/{{project_id}}/handoffs/{{handoff_id}}/claim",
        response_model=HandoffResponse,
        tags=["handoffs"],
    )
    def claim_handoff(
        project_id: str,
        handoff_id: str,
        payload: HandoffLeaseAction,
        identity: Principal = Depends(principal),
    ):
        return transition_handoff(
            "claim", project_id, handoff_id, payload, identity
        )

    @app.post(
        f"{API_PREFIX}/projects/{{project_id}}/handoffs/{{handoff_id}}/accept",
        response_model=HandoffResponse,
        tags=["handoffs"],
    )
    def accept_handoff(
        project_id: str,
        handoff_id: str,
        payload: HandoffLeaseAction,
        identity: Principal = Depends(principal),
    ):
        return transition_handoff(
            "accept", project_id, handoff_id, payload, identity
        )

    @app.post(
        f"{API_PREFIX}/projects/{{project_id}}/routes",
        response_model=QueueItemResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["routing"],
    )
    def route(
        project_id: str,
        payload: RouteCreate,
        identity: Principal = Depends(principal),
    ):
        project_access(identity, project_id, "write")
        selected_telemetry.annotate(
            project_id=project_id,
            work_item_id=payload.work_item_id,
        )
        return asdict(
            router.route(
                RouteDraft(
                    project_id=project_id,
                    work_item_id=payload.work_item_id,
                    target_role_id=payload.target_role_id,
                    capability=payload.capability,
                    idempotency_key=payload.idempotency_key,
                    payload=payload.payload,
                    priority=payload.priority,
                    available_at=payload.available_at,
                )
            )
        )

    @app.post(
        f"{API_PREFIX}/projects/{{project_id}}/queues/{{queue_id}}/items",
        response_model=QueueItemResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["queues"],
    )
    def enqueue(
        project_id: str,
        queue_id: str,
        payload: QueueItemCreate,
        identity: Principal = Depends(principal),
    ):
        project_access(identity, project_id, "write")
        selected_telemetry.annotate(
            project_id=project_id,
            work_item_id=payload.work_item_id,
            queue_id=queue_id,
        )
        return asdict(
            queues.enqueue(
                project_id=project_id,
                queue_id=queue_id,
                queue_item_id=payload.queue_item_id,
                work_item_id=payload.work_item_id,
                idempotency_key=payload.idempotency_key,
                payload=payload.payload,
                priority=payload.priority,
                available_at=payload.available_at,
            )
        )

    @app.post(
        f"{API_PREFIX}/projects/{{project_id}}/queues/{{queue_id}}/claim",
        response_model=LeaseClaimResponse | None,
        tags=["queues"],
    )
    def claim(
        project_id: str,
        queue_id: str,
        payload: QueueClaim,
        identity: Principal = Depends(principal),
    ):
        project_access(identity, project_id, "write")
        claimed = queues.claim(
            project_id=project_id,
            queue_id=queue_id,
            owner_instance_id=payload.owner_instance_id,
            lease_seconds=payload.lease_seconds,
        )
        return None if claimed is None else asdict(claimed)

    @app.post(
        f"{API_PREFIX}/projects/{{project_id}}/leases/{{lease_id}}/heartbeat",
        response_model=LeaseHeartbeatResponse,
        tags=["queues"],
    )
    def heartbeat(
        project_id: str,
        lease_id: str,
        payload: LeaseHeartbeat,
        identity: Principal = Depends(principal),
    ):
        project_access(identity, project_id, "write")
        expires_at = queues.heartbeat(
            project_id=project_id,
            lease_id=lease_id,
            lease_token=payload.lease_token,
            lease_seconds=payload.lease_seconds,
        )
        return {"expires_at": expires_at}

    def finish_lease(
        action: Literal["complete", "release"],
        project_id: str,
        lease_id: str,
        payload: LeaseFinish,
        identity: Principal,
    ):
        project_access(identity, project_id, "write")
        operation = queues.complete if action == "complete" else queues.release
        return asdict(
            operation(
                project_id=project_id,
                lease_id=lease_id,
                lease_token=payload.lease_token,
            )
        )

    @app.post(
        f"{API_PREFIX}/projects/{{project_id}}/leases/{{lease_id}}/complete",
        response_model=QueueItemResponse,
        tags=["queues"],
    )
    def complete_lease(
        project_id: str,
        lease_id: str,
        payload: LeaseFinish,
        identity: Principal = Depends(principal),
    ):
        return finish_lease("complete", project_id, lease_id, payload, identity)

    @app.post(
        f"{API_PREFIX}/projects/{{project_id}}/leases/{{lease_id}}/release",
        response_model=QueueItemResponse,
        tags=["queues"],
    )
    def release_lease(
        project_id: str,
        lease_id: str,
        payload: LeaseFinish,
        identity: Principal = Depends(principal),
    ):
        return finish_lease("release", project_id, lease_id, payload, identity)

    @app.get(
        f"{API_PREFIX}/projects/{{project_id}}/queues/{{queue_id}}/metrics",
        response_model=QueueMetricsResponse,
        tags=["queues"],
    )
    def queue_metrics(
        project_id: str,
        queue_id: str,
        identity: Principal = Depends(principal),
    ):
        project_access(identity, project_id, "read")
        return asdict(queues.metrics(project_id=project_id, queue_id=queue_id))

    @app.get(
        f"{API_PREFIX}/projects/{{project_id}}/queue-items/{{queue_item_id}}",
        response_model=QueueItemResponse,
        tags=["queues"],
    )
    def get_queue_item(
        project_id: str,
        queue_item_id: str,
        identity: Principal = Depends(principal),
    ):
        project_access(identity, project_id, "read")
        return asdict(queues.get_item(project_id, queue_item_id))

    @app.get(
        f"{API_PREFIX}/projects/{{project_id}}/agents",
        response_model=AgentsResponse,
        tags=["agents"],
    )
    def agents(project_id: str, identity: Principal = Depends(principal)):
        project_access(identity, project_id, "read")
        return queries.agents(project_id)

    def records(domain: str, project_id: str, identity: Principal):
        project_access(identity, project_id, "read")
        return {"records": queries.records(project_id, domain)}

    @app.get(f"{API_PREFIX}/projects/{{project_id}}/handoffs", response_model=RecordsResponse, tags=["handoffs"])
    def handoffs(project_id: str, identity: Principal = Depends(principal)):
        return records("handoffs", project_id, identity)

    @app.get(f"{API_PREFIX}/projects/{{project_id}}/progress", response_model=RecordsResponse, tags=["progress"])
    def progress(project_id: str, identity: Principal = Depends(principal)):
        return records("progress", project_id, identity)

    @app.post(
        f"{API_PREFIX}/projects/{{project_id}}/work-items/"
        "{work_item_id}/progress",
        response_model=ProgressResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["progress"],
    )
    def record_progress(
        project_id: str,
        work_item_id: str,
        payload: ProgressCreate,
        identity: Principal = Depends(principal),
    ):
        project_access(identity, project_id, "write")
        selected_telemetry.annotate(
            project_id=project_id,
            work_item_id=work_item_id,
        )
        return asdict(
            progress_store.record(
                ProgressDraft(
                    project_id=project_id,
                    work_item_id=work_item_id,
                    role_instance_id=payload.role_instance_id,
                    checkpoint_id=payload.checkpoint_id,
                    expected_previous_sequence=payload.expected_previous_sequence,
                    status=payload.status,
                    goal=payload.goal,
                    step=payload.step,
                    completed_action=payload.completed_action,
                    activity=payload.activity,
                    blocker=payload.blocker,
                    next_action=payload.next_action,
                    safe_summary=payload.safe_summary,
                )
            )
        )

    @app.get(f"{API_PREFIX}/projects/{{project_id}}/configuration", response_model=RecordsResponse, tags=["configuration"])
    def configuration(project_id: str, identity: Principal = Depends(principal)):
        return records("configuration", project_id, identity)

    @app.get(
        f"{API_PREFIX}/projects/{{project_id}}/configuration/promotion-state",
        tags=["configuration"],
    )
    def configuration_promotion_state(
        project_id: str, identity: Principal = Depends(principal)
    ):
        project_access(identity, project_id, "read")
        return configuration_promotions(project_id).activation.get_state().to_dict()

    @app.post(
        f"{API_PREFIX}/projects/{{project_id}}/configuration/drafts",
        status_code=status.HTTP_201_CREATED,
        tags=["configuration"],
    )
    def create_configuration_draft(
        project_id: str,
        payload: ConfigDraftCreate,
        identity: Principal = Depends(principal),
    ):
        project_access(identity, project_id, "write")
        return configuration_promotions(project_id).create(
            draft_id=payload.draft_id,
            references=payload.references,
            expected_active_digest=payload.expected_active_digest,
            actor=identity.subject,
        ).to_dict()

    @app.get(
        f"{API_PREFIX}/projects/{{project_id}}/configuration/drafts/{{draft_id}}",
        tags=["configuration"],
    )
    def get_configuration_draft(
        project_id: str,
        draft_id: str,
        identity: Principal = Depends(principal),
    ):
        project_access(identity, project_id, "read")
        return configuration_promotions(project_id).get(draft_id).to_dict()

    @app.post(
        f"{API_PREFIX}/projects/{{project_id}}/configuration/drafts/{{draft_id}}/validate",
        tags=["configuration"],
    )
    def validate_configuration_draft(
        project_id: str,
        draft_id: str,
        identity: Principal = Depends(principal),
    ):
        project_access(identity, project_id, "write")
        promotions = configuration_promotions(project_id)
        draft = promotions.get(draft_id)
        return promotions.validate(
            draft_id,
            actor=identity.subject,
            sponsor_authored=queries.is_sponsor(project_id, draft.created_by),
        ).to_dict()

    @app.post(
        f"{API_PREFIX}/projects/{{project_id}}/configuration/drafts/{{draft_id}}/decision",
        tags=["configuration"],
    )
    def decide_configuration_draft(
        project_id: str,
        draft_id: str,
        payload: ConfigDraftDecision,
        identity: Principal = Depends(principal),
    ):
        project_access(identity, project_id, "write")
        if not queries.is_sponsor(project_id, identity.subject):
            raise ControlApiError(
                403, "sponsor_required", "configuration decision requires a sponsor"
            )
        return configuration_promotions(project_id).decide(
            draft_id,
            sponsor_id=identity.subject,
            decision=payload.decision,
            rationale=payload.rationale,
        ).to_dict()

    @app.post(
        f"{API_PREFIX}/projects/{{project_id}}/configuration/drafts/{{draft_id}}/activate",
        tags=["configuration"],
    )
    def activate_configuration_draft(
        project_id: str,
        draft_id: str,
        payload: ConfigDraftActivation,
        identity: Principal = Depends(principal),
    ):
        project_access(identity, project_id, "write")
        return configuration_promotions(project_id).activate(
            draft_id, actor=identity.subject, reason=payload.reason
        ).to_dict()

    @app.post(
        f"{API_PREFIX}/projects/{{project_id}}/configuration/rollback",
        tags=["configuration"],
    )
    def rollback_configuration(
        project_id: str,
        payload: ConfigRollback,
        identity: Principal = Depends(principal),
    ):
        project_access(identity, project_id, "write")
        if not queries.is_sponsor(project_id, identity.subject):
            raise ControlApiError(
                403, "sponsor_required", "configuration rollback requires a sponsor"
            )
        return configuration_promotions(project_id).rollback(
            payload.target_digest,
            sponsor_id=identity.subject,
            reason=payload.reason,
        ).to_dict()

    @app.get(f"{API_PREFIX}/projects/{{project_id}}/audit", response_model=RecordsResponse, tags=["audit"])
    def audit(project_id: str, identity: Principal = Depends(principal)):
        return records("audit", project_id, identity)

    def planned_domain(
        domain: str, story: str, project_id: str, identity: Principal
    ) -> dict[str, Any]:
        project_access(identity, project_id, "read")
        queries.project(project_id)
        return {
            "domain": domain,
            "status": "planned",
            "planned_story": story,
            "available_operations": [],
        }

    @app.get(
        f"{API_PREFIX}/projects/{{project_id}}/usage",
        response_model=UsageSummaryResponse,
        tags=["usage"],
    )
    def usage(
        project_id: str,
        provider_id: str = Query(
            default="codex-local", min_length=1, max_length=128
        ),
        account_scope: str = Query(default="default", min_length=1, max_length=128),
        identity: Principal = Depends(principal),
    ):
        project_access(identity, project_id, "read")
        return usage_store.summary(
            project_id,
            provider_id=provider_id,
            account_scope=account_scope,
        )

    @app.get(
        f"{API_PREFIX}/dashboard/portfolio",
        response_model=DashboardPortfolioResponse,
        tags=["dashboard"],
    )
    def dashboard_portfolio(identity: Principal = Depends(principal)):
        scope_access(identity, "read")
        return dashboard_reads.portfolio(identity.projects)

    @app.get(
        f"{API_PREFIX}/projects/{{project_id}}/dashboard/work",
        response_model=DashboardPageResponse,
        tags=["dashboard"],
    )
    def dashboard_work(
        project_id: str,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        identity: Principal = Depends(principal),
    ):
        project_access(identity, project_id, "read")
        return dashboard_reads.work(project_id, limit=limit, offset=offset)

    @app.get(
        f"{API_PREFIX}/projects/{{project_id}}/dashboard/fleet",
        response_model=DashboardFleetResponse,
        tags=["dashboard"],
    )
    def dashboard_fleet(
        project_id: str, identity: Principal = Depends(principal)
    ):
        project_access(identity, project_id, "read")
        return dashboard_reads.fleet(project_id)

    @app.get(
        f"{API_PREFIX}/projects/{{project_id}}/dashboard/usage",
        response_model=DashboardUsageResponse,
        tags=["dashboard"],
    )
    def dashboard_usage(
        project_id: str,
        provider_id: str = Query(
            default="codex-local", min_length=1, max_length=128
        ),
        account_scope: str = Query(default="default", min_length=1, max_length=128),
        identity: Principal = Depends(principal),
    ):
        project_access(identity, project_id, "read")
        summary = usage_store.summary(
            project_id, provider_id=provider_id, account_scope=account_scope
        )
        return {
            "project_id": project_id,
            "traffic": usage_traffic(summary),
            "usage": summary,
        }

    @app.get(
        f"{API_PREFIX}/projects/{{project_id}}/dashboard/recovery",
        response_model=DashboardPageResponse,
        tags=["dashboard"],
    )
    def dashboard_recovery(
        project_id: str,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        identity: Principal = Depends(principal),
    ):
        project_access(identity, project_id, "read")
        return dashboard_reads.recovery(project_id, limit=limit, offset=offset)

    @app.get(
        f"{API_PREFIX}/projects/{{project_id}}/dashboard/audit",
        response_model=DashboardPageResponse,
        tags=["dashboard"],
    )
    def dashboard_audit(
        project_id: str,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        identity: Principal = Depends(principal),
    ):
        project_access(identity, project_id, "read")
        return dashboard_reads.audit(project_id, limit=limit, offset=offset)

    @app.get(
        f"{API_PREFIX}/projects/{{project_id}}/recovery",
        response_model=RecoveryOverviewResponse,
        tags=["recovery"],
    )
    def recovery(project_id: str, identity: Principal = Depends(principal)):
        project_access(identity, project_id, "read")
        queries.project(project_id)
        return {
            "project_id": project_id,
            "items": recovery_supervisor.list_project(project_id),
        }

    @app.get(
        f"{API_PREFIX}/projects/{{project_id}}/read-model",
        response_model=ReadModelSnapshotResponse,
        tags=["live"],
    )
    def read_model_snapshot(
        project_id: str, identity: Principal = Depends(principal)
    ):
        project_access(identity, project_id, "read")
        return read_models.snapshot(project_id)

    @app.get(
        f"{API_PREFIX}/projects/{{project_id}}/events",
        tags=["live"],
        responses={
            200: {
                "description": "Ordered project event stream",
                "content": {"text/event-stream": {}},
            }
        },
    )
    async def live_events(
        request: Request,
        project_id: str,
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
        once: bool = False,
        limit: int = Query(default=100, ge=1, le=1000),
        identity: Principal = Depends(principal),
    ):
        project_access(identity, project_id, "read")
        cursor = _event_cursor(last_event_id)
        await anyio.to_thread.run_sync(read_models.require_project, project_id)

        async def generate():
            current = cursor
            idle_polls = 0
            while True:
                if await request.is_disconnected():
                    return
                batch = await anyio.to_thread.run_sync(
                    partial(
                        read_models.events,
                        project_id,
                        after_event_id=current,
                        limit=limit,
                    )
                )
                if batch:
                    idle_polls = 0
                    for event in batch:
                        if await request.is_disconnected():
                            return
                        yield event.to_sse()
                        current = event.event_id
                    if once:
                        return
                    continue
                if once:
                    return
                idle_polls += 1
                if idle_polls >= 15:
                    yield ": keep-alive\n\n"
                    idle_polls = 0
                await anyio.sleep(1)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


def app_from_environment() -> FastAPI:
    database_url = database_url_from_environment()
    root = os.environ.get(CONFIG_ROOT_ENV, "").strip()
    config_resolver = (
        None
        if not root
        else lambda _project_id: ConfigActivationStore(Path(root))
    )
    flow_resolver = (
        None if not root else _project_flow_resolver(database_url, Path(root))
    )
    fleet_map = os.environ.get("AGENTIC_MESH_V5_FLEET_MAP", "").strip()
    fleet_supervisor = (
        None
        if not fleet_map
        else DockerContainerFleetSupervisor(Path(fleet_map))
    )
    document_store_resolver = _document_store_resolver_from_environment(database_url)
    return create_app(
        database_url,
        telemetry=telemetry_from_environment(),
        fleet_supervisor=fleet_supervisor,
        flow_resolver=flow_resolver,
        document_store_resolver=document_store_resolver,
        config_store_resolver=config_resolver,
    )


def _document_store_resolver_from_environment(
    database_url: str,
) -> Callable[[str], DocumentStore] | None:
    credential_root = os.environ.get(DOCUMENT_CREDENTIAL_ROOT_ENV, "").strip()
    root_id = os.environ.get(DOCUMENT_ROOT_ID_ENV, "").strip()
    if not credential_root and not root_id:
        return None
    if not credential_root or not root_id:
        raise ValueError(
            f"{DOCUMENT_CREDENTIAL_ROOT_ENV} and {DOCUMENT_ROOT_ID_ENV} "
            "must be configured together"
        )
    if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,127}", root_id) is None:
        raise ValueError(f"{DOCUMENT_ROOT_ID_ENV} is invalid")
    factory = OneDriveDocumentStoreFactory(
        database_url,
        token_provider=MountedAccessTokenProvider(Path(credential_root)),
        transport=HttpxTransport(),
    )
    return lambda project_id: factory.create(project_id=project_id, root_id=root_id)


def _project_flow_resolver(
    database_url: str, configuration_root: Path
) -> Callable[[str], FlowDefinition]:
    def resolve(project_id: str) -> FlowDefinition:
        with psycopg.connect(database_url, autocommit=True) as connection:
            rows = connection.execute(
                f"SELECT DISTINCT flow_reference FROM {SCHEMA}.role_bindings "
                "WHERE project_id = %s",
                (project_id,),
            ).fetchall()
        if len(rows) != 1:
            raise FlowDefinitionError("project flow configuration is unavailable")
        try:
            return load_flow(resolve_packages(configuration_root, [rows[0][0]]))
        except PackageResolutionError as exc:
            raise FlowDefinitionError(
                "project flow configuration is unavailable"
            ) from exc

    return resolve


def _request_route(request: Request) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else "unmatched"


def _problem_response(
    request: Request,
    status_code: int,
    code: str,
    detail: str,
    *,
    errors: list[dict[str, Any]] | None = None,
    authenticate: bool = False,
) -> JSONResponse:
    request_id = getattr(request.state, "request_id", uuid.uuid4().hex)
    content = Problem(
        type=f"urn:agentic-mesh:error:{code}",
        title=code.replace("_", " ").title(),
        status=status_code,
        detail=detail,
        instance=request.url.path,
        request_id=request_id,
        errors=errors,
    ).model_dump(exclude_none=True)
    headers = {"WWW-Authenticate": "Bearer"} if authenticate else None
    return JSONResponse(
        status_code=status_code,
        content=content,
        media_type="application/problem+json",
        headers=headers,
    )


def _event_cursor(value: str | None) -> int:
    if value is None:
        return 0
    if re.fullmatch(r"[0-9]+", value) is None:
        raise ValueError("Last-Event-ID must be a non-negative integer")
    return int(value)
