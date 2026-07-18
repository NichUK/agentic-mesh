from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime
from functools import partial
import re
from typing import Any, Literal
import uuid

import anyio
from fastapi import Depends, FastAPI, Header, Query, Request, Response, Security, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
import psycopg
from psycopg.rows import dict_row

from agentic_mesh_v5 import __version__
from agentic_mesh_v5.api_auth import Principal
from agentic_mesh_v5.api_auth import TokenAuthorizer
from agentic_mesh_v5.database import DatabaseConfigurationError
from agentic_mesh_v5.database import DatabaseError
from agentic_mesh_v5.database import SCHEMA
from agentic_mesh_v5.database import database_url_from_environment
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
from agentic_mesh_v5.progress import ProgressConflict
from agentic_mesh_v5.progress import ProgressDraft
from agentic_mesh_v5.progress import ProgressNotFound
from agentic_mesh_v5.progress import ProgressStore
from agentic_mesh_v5.queues import LeaseExpired
from agentic_mesh_v5.queues import QueueAuthorizationError
from agentic_mesh_v5.queues import QueueConflict
from agentic_mesh_v5.queues import QueueNotFound
from agentic_mesh_v5.queues import RoleQueueStore
from agentic_mesh_v5.read_models import ReadModelNotFound
from agentic_mesh_v5.read_models import ReadModelStore
from agentic_mesh_v5.routing import RouteDraft
from agentic_mesh_v5.routing import Router
from agentic_mesh_v5.routing import RoutingConflict
from agentic_mesh_v5.routing import RoutingNotFound
from agentic_mesh_v5.telemetry import Telemetry
from agentic_mesh_v5.telemetry import telemetry_from_environment
from agentic_mesh_v5.usage import UsageNotFound
from agentic_mesh_v5.usage import UsageStore


API_PREFIX = "/api/v1"
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


class GateOpen(ApiModel):
    gate_id: str = Field(min_length=1)
    gate_type: str = Field(min_length=1)
    sponsor_ids: list[str] = Field(min_length=1)
    correlation_id: str = Field(min_length=1)
    expected_version: int = Field(ge=1)
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
                       started_at, heartbeat_at, hibernated_at
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
    authorizer: TokenAuthorizer | None = None,
    telemetry: Telemetry | None = None,
) -> FastAPI:
    selected_authorizer = authorizer or TokenAuthorizer.from_environment()
    selected_telemetry = telemetry or Telemetry()
    lifecycle = LifecycleStore(database_url)
    progress_store = ProgressStore(database_url)
    usage_store = UsageStore(database_url)
    queues = RoleQueueStore(database_url)
    router = Router(database_url)
    handoff_store = HandoffStore(database_url)
    queries = ControlQueries(database_url)
    read_models = ReadModelStore(database_url)
    health_reporter = HealthReporter(database_url, selected_telemetry)
    bearer = HTTPBearer(auto_error=False)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
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
    async def not_found(request: Request, exc: Exception) -> JSONResponse:
        return _problem_response(request, 404, "not_found", str(exc))

    @app.exception_handler(LifecycleConflict)
    @app.exception_handler(QueueConflict)
    @app.exception_handler(LeaseExpired)
    @app.exception_handler(ProgressConflict)
    @app.exception_handler(RoutingConflict)
    @app.exception_handler(HandoffConflict)
    async def conflict(request: Request, exc: Exception) -> JSONResponse:
        return _problem_response(request, 409, "conflict", str(exc))

    @app.exception_handler(LifecycleAuthorizationError)
    @app.exception_handler(QueueAuthorizationError)
    @app.exception_handler(HandoffAuthorizationError)
    async def forbidden(request: Request, exc: Exception) -> JSONResponse:
        return _problem_response(request, 403, "operation_forbidden", str(exc))

    @app.exception_handler(ValueError)
    async def invalid_value(request: Request, exc: ValueError) -> JSONResponse:
        return _problem_response(request, 422, "validation_failed", str(exc))

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
        identity: Principal = Depends(principal),
    ):
        project_access(identity, project_id, "write")
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

    @app.get(f"{API_PREFIX}/projects/{{project_id}}/recovery", response_model=DomainAvailability, tags=["recovery"])
    def recovery(project_id: str, identity: Principal = Depends(principal)):
        return planned_domain("recovery", "AMV5-034", project_id, identity)

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
    return create_app(
        database_url_from_environment(), telemetry=telemetry_from_environment()
    )


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
