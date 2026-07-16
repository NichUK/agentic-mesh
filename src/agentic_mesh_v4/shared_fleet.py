from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
import hashlib
import json
from pathlib import Path
import re
from typing import Any
from typing import Iterable
from typing import Mapping
from typing import Sequence

from agentic_mesh_v4.config import V4ProjectAssignmentConfig
from agentic_mesh_v4.config import V4ProjectConfig


CANONICAL_FLEET_ID = "agentic-mesh"
SHARED_FLEET_SCHEMA_VERSION = 1
STAGE1_DISPOSABLE_PERMIT = "stage1-disposable-only"
RECORD_ACTIONS = frozenset(
    {
        "annotate_assignment",
        "duplicate_noop",
        "hard_conflict",
        "retain_binding_reference",
        "retain_history",
        "skipped_absent",
        "skipped_sensitive",
        "skipped_unknown_identity",
    }
)

STATE_LEDGER: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("physical_identity_and_assignment", ("role_instances",)),
    ("sessions_and_codex_execution", ("agent_sessions", "codex_threads", "codex_turns")),
    ("queue_and_journal", ("message_queue", "message_journal")),
    ("runtime_and_completion", ("agent_events", "turn_completion_diagnostics")),
    ("safe_output_and_preflight", ("safe_output_calls", "preflight_results")),
    ("memory_and_summaries", ("role_memory", "project_memory", "summaries")),
    (
        "work_and_governance",
        ("work_items", "architecture_governance_records", "approvals", "artifacts", "releases"),
    ),
    ("documents", ("document_revisions", "document_merge_tasks", "document_write_warnings")),
    (
        "decisions_and_notifications",
        (
            "decision_records",
            "decision_card_deliveries",
            "decision_callbacks",
            "decision_links",
            "decision_notification_recovery_summaries",
            "decision_notification_outcomes",
            "decision_notification_outcome_events",
        ),
    ),
    ("handoffs", ("handoffs", "handoff_transitions")),
    ("watchdog", ("watchdog_sweep_runs", "watchdog_findings")),
)
PUBLIC_ONLY_LEDGER: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("public_only_document_evidence", ("document_revision_contents", "document_write_attempts")),
    (
        "public_only_cleanup_audit",
        ("pm_handoff_cleanup_audit_20260713", "pm_queue_cleanup_audit_20260713"),
    ),
)
FILESYSTEM_STATE_CLASSES: tuple[str, ...] = (
    "project_documents",
    "repositories_and_workspaces",
    "codex_role_state",
    "project_and_role_configuration",
    "compose_and_service_state",
    "database_globals_and_credentials",
    "dashboard_and_runtime_projection",
)
COMMON_TABLES = frozenset(table for _state_class, tables in STATE_LEDGER for table in tables)
PUBLIC_ONLY_TABLES = frozenset(table for _state_class, tables in PUBLIC_ONLY_LEDGER for table in tables)
ALL_PUBLIC_TABLES = COMMON_TABLES | PUBLIC_ONLY_TABLES
_TABLE_TO_CLASS = {
    table: state_class
    for state_class, tables in (*STATE_LEDGER, *PUBLIC_ONLY_LEDGER)
    for table in tables
}
_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_TERMINAL_MESSAGE_STATES = {
    "blocked",
    "blocked_preflight",
    "cancelled",
    "completed",
    "completed_with_exception",
    "completed_with_missing_evidence_warning",
    "completed_with_missing_output",
    "dead_lettered",
    "failed",
    "failed_preflight",
    "steered",
    "superseded",
}


class SharedFleetError(RuntimeError):
    pass


class SharedFleetConflict(SharedFleetError):
    pass


class SharedFleetActivationClosed(SharedFleetError):
    pass


def stable_fleet_instance_id(*, fleet_id: str, role_id: str, ordinal: int = 1) -> str:
    _validate_fleet_and_role(fleet_id=fleet_id, role_id=role_id, ordinal=ordinal)
    return f"{fleet_id}.{role_id}.{ordinal}"


def stable_fleet_service_name(*, fleet_id: str, role_id: str, ordinal: int = 1) -> str:
    _validate_fleet_and_role(fleet_id=fleet_id, role_id=role_id, ordinal=ordinal)
    return f"{fleet_id}-{role_id}-{ordinal}"


def require_stage1_default_off(project_config: V4ProjectConfig, *, operation: str) -> None:
    if project_config.shared_fleet.enabled:
        raise SharedFleetActivationClosed(
            f"shared fleet activation is closed during Stage 1: {operation}"
        )


def generated_shared_fleet_plan(project_config: V4ProjectConfig) -> dict[str, Any]:
    require_stage1_default_off(project_config, operation="configuration materialization")
    fleet_id = project_config.shared_fleet.fleet_id
    instances = []
    for role in project_config.roles:
        for ordinal in range(1, role.instances + 1):
            instances.append(
                {
                    "fleet_instance_id": stable_fleet_instance_id(
                        fleet_id=fleet_id,
                        role_id=role.role_id,
                        ordinal=ordinal,
                    ),
                    "ordinal": ordinal,
                    "role_id": role.role_id,
                    "service_name": stable_fleet_service_name(
                        fleet_id=fleet_id,
                        role_id=role.role_id,
                        ordinal=ordinal,
                    ),
                }
            )
    assignments = [
        {
            "codex_home": assignment.codex_home,
            "database_credential_ref": assignment.database_credential_ref,
            "database_schema": assignment.database_schema,
            "document_root": assignment.document_root,
            "project_id": assignment.project_id,
            "project_root": assignment.project_root,
            "repository_roots": list(assignment.repository_roots),
            "workspace_root": assignment.workspace_root,
        }
        for assignment in project_config.shared_fleet.project_assignments
    ]
    return {
        "activation_gate": "stages_2_4_closed",
        "enabled": False,
        "fleet_id": fleet_id,
        "physical_instances": sorted(instances, key=lambda item: item["fleet_instance_id"]),
        "project_assignments": sorted(assignments, key=lambda item: item["project_id"]),
        "runnable": False,
        "schema_version": SHARED_FLEET_SCHEMA_VERSION,
    }


@dataclass(frozen=True)
class FleetActivity:
    active_turn: bool = False
    claimed_message: bool = False
    safe_output_active: bool = False
    lifecycle_active: bool = False

    @property
    def idle(self) -> bool:
        return not any(
            (self.active_turn, self.claimed_message, self.safe_output_active, self.lifecycle_active)
        )


@dataclass(frozen=True)
class FleetBinding:
    fleet_instance_id: str
    project_id: str | None = None
    generation: int = 0
    state: str = "unbound"


@dataclass(frozen=True)
class BoundProjectContext:
    fleet_instance_id: str
    project_id: str
    generation: int
    database_schema: str
    database_credential_ref: str
    document_root: str
    project_root: str
    workspace_root: str
    repository_roots: tuple[str, ...]
    codex_home: str

    def require_database(self, *, schema: str, credential_ref: str) -> None:
        if schema != self.database_schema:
            raise SharedFleetConflict("foreign database schema denied by project binding")
        if credential_ref != self.database_credential_ref:
            raise SharedFleetConflict("foreign database credential denied by project binding")

    def require_path(self, path: str | Path, *, kind: str) -> Path:
        candidate = Path(path).resolve()
        roots = {
            "document": (Path(self.document_root).resolve(),),
            "project": (Path(self.project_root).resolve(),),
            "workspace": (Path(self.workspace_root).resolve(),),
            "repository": tuple(Path(item).resolve() for item in self.repository_roots),
            "codex_home": (Path(self.codex_home).resolve(),),
        }.get(kind)
        if roots is None:
            raise ValueError(f"unknown shared-fleet path kind: {kind}")
        if not any(candidate == root or root in candidate.parents for root in roots):
            raise SharedFleetConflict(f"foreign {kind} mount denied by project binding")
        return candidate


class CapturedBindingController:
    """Pure Stage 1 model. It never starts services, mounts paths, or opens a database."""

    def __init__(self, assignments: Sequence[V4ProjectAssignmentConfig]) -> None:
        self._assignments = {assignment.project_id: assignment for assignment in assignments}
        if len(self._assignments) != len(assignments):
            raise SharedFleetConflict("duplicate project assignment")

    def bind(
        self,
        binding: FleetBinding,
        *,
        project_id: str,
        activity: FleetActivity,
    ) -> tuple[FleetBinding, BoundProjectContext]:
        if not activity.idle:
            raise SharedFleetConflict("fleet instance must be idle before project binding")
        if binding.state != "unbound" or binding.project_id is not None:
            raise SharedFleetConflict("fleet instance must be unbound before project binding")
        assignment = self._assignments.get(project_id)
        if assignment is None:
            raise SharedFleetConflict(f"unknown project assignment: {project_id}")
        generation = binding.generation + 1
        updated = FleetBinding(
            fleet_instance_id=binding.fleet_instance_id,
            project_id=project_id,
            generation=generation,
            state="bound",
        )
        return updated, _bound_context(updated, assignment)

    def unbind(self, binding: FleetBinding, *, activity: FleetActivity) -> FleetBinding:
        if not activity.idle:
            raise SharedFleetConflict("fleet instance must be idle before unbinding")
        return FleetBinding(
            fleet_instance_id=binding.fleet_instance_id,
            generation=binding.generation,
        )

    def require_operation(
        self,
        binding: FleetBinding,
        *,
        project_id: str | None,
        generation: int | None,
    ) -> BoundProjectContext:
        if binding.state != "bound" or binding.project_id is None:
            raise SharedFleetConflict("shared fleet operation requires a bound project")
        if project_id != binding.project_id:
            raise SharedFleetConflict("shared fleet project binding mismatch")
        if generation != binding.generation:
            raise SharedFleetConflict("shared fleet binding generation mismatch")
        assignment = self._assignments.get(binding.project_id)
        if assignment is None:
            raise SharedFleetConflict("bound project assignment is unavailable")
        return _bound_context(binding, assignment)


@dataclass(frozen=True)
class SharedFleetOperationGuard:
    """Dormant production-seam hook carrying one immutable captured binding."""

    controller: CapturedBindingController
    binding: FleetBinding

    def require(
        self,
        *,
        project_id: str | None,
        generation: int | None,
    ) -> BoundProjectContext:
        return self.controller.require_operation(
            self.binding,
            project_id=project_id,
            generation=generation,
        )

    def require_database(
        self,
        *,
        project_id: str | None,
        generation: int | None,
        schema: str,
        credential_ref: str,
    ) -> BoundProjectContext:
        context = self.require(project_id=project_id, generation=generation)
        context.require_database(schema=schema, credential_ref=credential_ref)
        return context


@dataclass(frozen=True)
class SourceRoleIdentity:
    project_id: str
    role_instance_id: str
    role_id: str
    ordinal: int
    canonical_role_fingerprint: str


@dataclass(frozen=True)
class ExistingFleetIdentity:
    fleet_instance_id: str
    canonical_role_fingerprint: str


@dataclass(frozen=True)
class IdentityAction:
    action: str
    fleet_instance_id: str
    project_id: str | None
    source_role_instance_id: str | None
    reason: str


@dataclass(frozen=True)
class IdentityReconciliation:
    actions: tuple[IdentityAction, ...]
    blocking: bool


def reconcile_identities(
    *,
    fleet_id: str,
    sources: Sequence[SourceRoleIdentity],
    existing: Mapping[str, ExistingFleetIdentity] | None = None,
) -> IdentityReconciliation:
    existing = existing or {}
    actions: list[IdentityAction] = []
    blocking = False
    grouped: dict[str, list[SourceRoleIdentity]] = {}
    fingerprints: dict[str, str] = {}
    seen_assignments: set[tuple[str, str]] = set()
    for source in sources:
        expected_source_id = f"{source.project_id}.{source.role_id}.{source.ordinal}"
        fleet_instance_id = stable_fleet_instance_id(
            fleet_id=fleet_id,
            role_id=source.role_id,
            ordinal=source.ordinal,
        )
        if source.role_instance_id != expected_source_id:
            blocking = True
            actions.append(
                IdentityAction(
                    action="skipped_unknown_identity",
                    fleet_instance_id=fleet_instance_id,
                    project_id=source.project_id,
                    source_role_instance_id=source.role_instance_id,
                    reason="source_identity_not_canonical",
                )
            )
            continue
        assignment_key = (source.project_id, fleet_instance_id)
        if assignment_key in seen_assignments:
            blocking = True
            actions.append(
                IdentityAction(
                    action="hard_conflict",
                    fleet_instance_id=fleet_instance_id,
                    project_id=source.project_id,
                    source_role_instance_id=source.role_instance_id,
                    reason="duplicate_project_assignment",
                )
            )
            continue
        seen_assignments.add(assignment_key)
        prior_fingerprint = fingerprints.get(fleet_instance_id)
        if prior_fingerprint is not None and prior_fingerprint != source.canonical_role_fingerprint:
            blocking = True
            actions.append(
                IdentityAction(
                    action="hard_conflict",
                    fleet_instance_id=fleet_instance_id,
                    project_id=source.project_id,
                    source_role_instance_id=source.role_instance_id,
                    reason="canonical_role_fingerprint_mismatch",
                )
            )
            continue
        fingerprints[fleet_instance_id] = source.canonical_role_fingerprint
        grouped.setdefault(fleet_instance_id, []).append(source)
        actions.append(
            IdentityAction(
                action="expected_project_assignment",
                fleet_instance_id=fleet_instance_id,
                project_id=source.project_id,
                source_role_instance_id=source.role_instance_id,
                reason="project_state_retained_in_place",
            )
        )
    for fleet_instance_id, group in grouped.items():
        target = existing.get(fleet_instance_id)
        fingerprint = fingerprints[fleet_instance_id]
        if target is None:
            action = "create_fleet_instance"
            reason = "stable_physical_identity_absent"
        elif target.canonical_role_fingerprint == fingerprint:
            action = "duplicate_noop"
            reason = "stable_physical_identity_matches"
        else:
            action = "hard_conflict"
            reason = "stable_physical_identity_diverges"
            blocking = True
        actions.append(
            IdentityAction(
                action=action,
                fleet_instance_id=fleet_instance_id,
                project_id=None,
                source_role_instance_id=None,
                reason=reason,
            )
        )
    return IdentityReconciliation(
        actions=tuple(
            sorted(
                actions,
                key=lambda item: (
                    item.fleet_instance_id,
                    item.project_id or "",
                    item.source_role_instance_id or "",
                    item.action,
                ),
            )
        ),
        blocking=blocking,
    )


@dataclass(frozen=True)
class CatalogSnapshot:
    project_id: str
    schema: str
    control_schema: bool
    table_counts: Mapping[str, int]
    records: Mapping[str, tuple["CatalogRecord", ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class CatalogRecord:
    source_key: Mapping[str, Any]
    allowed_metadata: Mapping[str, Any]
    target_key: Mapping[str, Any] | None = None
    target_allowed_metadata: Mapping[str, Any] | None = None
    action: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class FilesystemStateSnapshot:
    project_id: str
    state_class: str
    source_key: Mapping[str, Any]
    allowed_metadata: Mapping[str, Any]
    target_key: Mapping[str, Any] | None = None
    target_allowed_metadata: Mapping[str, Any] | None = None
    action: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class ReconciliationManifest:
    payload: Mapping[str, Any]
    digest: str
    blocking: bool

    def as_dict(self) -> dict[str, Any]:
        return {**self.payload, "digest": self.digest}


def build_reconciliation_manifest(
    *,
    catalogs: Sequence[CatalogSnapshot],
    identities: IdentityReconciliation,
    filesystem_state: Sequence[FilesystemStateSnapshot] = (),
) -> ReconciliationManifest:
    conflicts: list[dict[str, str]] = []
    table_entries: list[dict[str, Any]] = []
    for catalog in sorted(catalogs, key=lambda item: (item.project_id, item.schema)):
        expected = ALL_PUBLIC_TABLES if catalog.control_schema else COMMON_TABLES
        discovered = set(catalog.table_counts)
        for table in sorted(expected - discovered):
            conflicts.append(
                {
                    "project_id": catalog.project_id,
                    "reason": "missing_expected_table",
                    "schema": catalog.schema,
                    "table": table,
                }
            )
        for table in sorted(discovered - expected):
            conflicts.append(
                {
                    "project_id": catalog.project_id,
                    "reason": "unexpected_table",
                    "schema": catalog.schema,
                    "table": table,
                }
            )
        for table in sorted(discovered & expected):
            records = tuple(catalog.records.get(table, ()))
            expected_count = int(catalog.table_counts[table])
            if len(records) != expected_count:
                conflicts.append(
                    {
                        "project_id": catalog.project_id,
                        "reason": "record_inventory_incomplete",
                        "schema": catalog.schema,
                        "table": table,
                    }
                )
            record_entries = sorted(
                [
                _record_manifest_entry(
                    project_id=catalog.project_id,
                    schema=catalog.schema,
                    state_class=_TABLE_TO_CLASS[table],
                    table=table,
                    record=record,
                    default_action=(
                        "annotate_assignment" if table == "role_instances" else "retain_history"
                    ),
                )
                for record in records
                ],
                key=lambda item: (
                    item["qualified_source_key_digest"],
                    item["qualified_target_key_digest"],
                    item["action"],
                ),
            )
            if any(item["action"] == "hard_conflict" for item in record_entries):
                conflicts.extend(
                    {
                        "project_id": catalog.project_id,
                        "reason": str(item["reason"]),
                        "schema": catalog.schema,
                        "table": table,
                    }
                    for item in record_entries
                    if item["action"] == "hard_conflict"
                )
            table_entries.append(
                {
                    "action": "retain_history" if table != "role_instances" else "annotate_assignment",
                    "project_id": catalog.project_id,
                    "record_actions": record_entries,
                    "retained_state_digest": _digest(record_entries),
                    "row_count": expected_count,
                    "schema": catalog.schema,
                    "state_class": _TABLE_TO_CLASS[table],
                    "table": table,
                }
            )
        if not catalog.control_schema:
            for table in sorted(PUBLIC_ONLY_TABLES):
                table_entries.append(
                    {
                        "action": "skipped_absent",
                        "project_id": catalog.project_id,
                        "record_actions": [],
                        "retained_state_digest": _digest([]),
                        "row_count": 0,
                        "schema": catalog.schema,
                        "state_class": _TABLE_TO_CLASS[table],
                        "table": table,
                    }
                )
    identity_entries = [
        {
            "action": action.action,
            "fleet_instance_id": action.fleet_instance_id,
            "project_id": action.project_id,
            "reason": action.reason,
            "source_role_instance_id": action.source_role_instance_id,
        }
        for action in identities.actions
    ]
    filesystem_entries = _filesystem_manifest_entries(filesystem_state)
    conflicts.extend(
        {
            "project_id": str(item["project_id"]),
            "reason": str(item["reason"]),
            "schema": "filesystem_config",
            "table": str(item["state_class"]),
        }
        for item in filesystem_entries
        if item["action"] == "hard_conflict"
    )
    action_counts: dict[str, int] = {}
    for item in identity_entries:
        action_counts[item["action"]] = action_counts.get(item["action"], 0) + 1
    for table in table_entries:
        if table["record_actions"]:
            for item in table["record_actions"]:
                action_counts[item["action"]] = action_counts.get(item["action"], 0) + 1
        else:
            action_counts[table["action"]] = action_counts.get(table["action"], 0) + 1
    for item in filesystem_entries:
        action_counts[item["action"]] = action_counts.get(item["action"], 0) + 1
    blocking = bool(conflicts) or identities.blocking
    retained_by_class: dict[str, list[dict[str, Any]]] = {}
    for table in table_entries:
        retained_by_class.setdefault(str(table["state_class"]), []).append(
            {
                "project_id": table["project_id"],
                "row_count": table["row_count"],
                "retained_state_digest": table["retained_state_digest"],
                "schema": table["schema"],
                "table": table["table"],
            }
        )
    retained_state = [
        {
            "digest": _digest(entries),
            "row_count": sum(int(item["row_count"]) for item in entries),
            "state_class": state_class,
        }
        for state_class, entries in sorted(retained_by_class.items())
    ]
    payload: dict[str, Any] = {
        "action_counts": dict(sorted(action_counts.items())),
        "blocking": blocking,
        "catalogs": table_entries,
        "conflicts": conflicts,
        "filesystem_state": filesystem_entries,
        "identity_actions": identity_entries,
        "retained_state": retained_state,
        "schema_version": SHARED_FLEET_SCHEMA_VERSION,
        "terminal": True,
        "terminal_status": "blocked" if blocking else "ready_for_authorization_review",
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return ReconciliationManifest(
        payload=payload,
        digest=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        blocking=bool(payload["blocking"]),
    )


def discover_postgres_catalog(connection: Any, *, project_id: str, schema: str, control_schema: bool) -> CatalogSnapshot:
    from psycopg import sql

    raw_connection = getattr(connection, "_connection", connection)
    rows = raw_connection.execute(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema=%s AND table_type='BASE TABLE'
        ORDER BY table_name
        """,
        (schema,),
    ).fetchall()
    tables = [str(row["table_name"] if isinstance(row, Mapping) else row[0]) for row in rows]
    counts: dict[str, int] = {}
    records: dict[str, tuple[CatalogRecord, ...]] = {}
    for table in tables:
        columns = _table_columns(raw_connection, schema=schema, table=table)
        key_columns = _primary_key_columns(raw_connection, schema=schema, table=table)
        if not key_columns:
            key_columns = tuple(column for column in columns if column.endswith("_id"))[:1]
        if not key_columns:
            raise SharedFleetConflict(f"content-free catalog requires a qualified key: {schema}.{table}")
        metadata_columns = tuple(
            column for column in columns if column not in key_columns and _allowed_metadata_column(column)
        )
        selected = (*key_columns, *metadata_columns)
        query = sql.SQL("SELECT {} FROM {}.{} ORDER BY {}").format(
            sql.SQL(", ").join(sql.Identifier(column) for column in selected),
            sql.Identifier(schema),
            sql.Identifier(table),
            sql.SQL(", ").join(sql.Identifier(column) for column in key_columns),
        )
        rows_for_table = raw_connection.execute(query).fetchall()
        records[table] = tuple(
            CatalogRecord(
                source_key={column: _row_value(row, column, index) for index, column in enumerate(key_columns)},
                allowed_metadata={
                    column: _row_value(row, column, len(key_columns) + index)
                    for index, column in enumerate(metadata_columns)
                },
            )
            for row in rows_for_table
        )
        counts[table] = len(rows_for_table)
    return CatalogSnapshot(
        project_id=project_id,
        schema=schema,
        control_schema=control_schema,
        table_counts=counts,
        records=records,
    )


def _record_manifest_entry(
    *,
    project_id: str,
    schema: str,
    state_class: str,
    table: str,
    record: CatalogRecord | FilesystemStateSnapshot,
    default_action: str,
) -> dict[str, Any]:
    source_key_digest = _digest(
        {
            "project_id": project_id,
            "schema": schema,
            "state_class": state_class,
            "table": table,
            "key": record.source_key,
        }
    )
    target_key = record.target_key if record.target_key is not None else record.source_key
    target_key_digest = _digest(
        {
            "project_id": project_id,
            "schema": schema,
            "state_class": state_class,
            "table": table,
            "key": target_key,
        }
    )
    source_metadata_digest = _digest(record.allowed_metadata)
    target_metadata_digest = (
        _digest(record.target_allowed_metadata)
        if record.target_allowed_metadata is not None
        else None
    )
    action = record.action
    reason = record.reason
    if action is None and record.target_allowed_metadata is not None:
        if source_key_digest != target_key_digest:
            action = "hard_conflict"
            reason = "qualified_target_key_diverges"
        elif source_metadata_digest != target_metadata_digest:
            action = "hard_conflict"
            reason = "allowed_metadata_content_conflict"
        else:
            action = "duplicate_noop"
            reason = "qualified_key_and_allowed_metadata_match"
    if action is None:
        action = default_action
        reason = reason or "state_retained_in_project_context"
    if action not in RECORD_ACTIONS:
        raise SharedFleetConflict(f"unknown closed reconciliation action: {action}")
    return {
        "action": action,
        "allowed_metadata_digest": source_metadata_digest,
        "qualified_source_key_digest": source_key_digest,
        "qualified_target_key_digest": target_key_digest,
        "reason": reason,
        "target_allowed_metadata_digest": target_metadata_digest,
    }


def _filesystem_manifest_entries(
    snapshots: Sequence[FilesystemStateSnapshot],
) -> list[dict[str, Any]]:
    grouped = {item.state_class: item for item in snapshots}
    if len(grouped) != len(snapshots):
        raise SharedFleetConflict("duplicate filesystem/configuration state class")
    unknown = set(grouped) - set(FILESYSTEM_STATE_CLASSES)
    if unknown:
        raise SharedFleetConflict(f"unknown filesystem/configuration state classes: {sorted(unknown)}")
    entries: list[dict[str, Any]] = []
    for state_class in FILESYSTEM_STATE_CLASSES:
        record = grouped.get(state_class)
        if record is None:
            record = FilesystemStateSnapshot(
                project_id="unassigned",
                state_class=state_class,
                source_key={"state_class": state_class},
                allowed_metadata={"captured": False},
                action="retain_binding_reference",
                reason="class_mapped_without_content_inspection",
            )
        entry = _record_manifest_entry(
            project_id=record.project_id,
            schema="filesystem_config",
            state_class=state_class,
            table=state_class,
            record=record,
            default_action="retain_binding_reference",
        )
        entries.append({"project_id": record.project_id, "state_class": state_class, **entry})
    return entries


def _digest(value: Any) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), default=_json_scalar)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _json_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _table_columns(connection: Any, *, schema: str, table: str) -> tuple[str, ...]:
    rows = connection.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema=%s AND table_name=%s
        ORDER BY ordinal_position
        """,
        (schema, table),
    ).fetchall()
    return tuple(str(row["column_name"] if isinstance(row, Mapping) else row[0]) for row in rows)


def _primary_key_columns(connection: Any, *, schema: str, table: str) -> tuple[str, ...]:
    rows = connection.execute(
        """
        SELECT kcu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name=kcu.constraint_name
         AND tc.table_schema=kcu.table_schema
         AND tc.table_name=kcu.table_name
        WHERE tc.table_schema=%s AND tc.table_name=%s
          AND tc.constraint_type='PRIMARY KEY'
        ORDER BY kcu.ordinal_position
        """,
        (schema, table),
    ).fetchall()
    return tuple(str(row["column_name"] if isinstance(row, Mapping) else row[0]) for row in rows)


def _allowed_metadata_column(column: str) -> bool:
    forbidden = {
        "content",
        "detail",
        "error",
        "goal",
        "message",
        "next_action",
        "payload",
        "prompt",
        "rationale",
        "result",
        "summary",
        "text",
        "title",
    }
    if column in forbidden or column.endswith("_json") or column.endswith("_text"):
        return False
    return (
        column in {"authority", "durable", "identity_kind", "role_id", "scope", "steering"}
        or column.endswith("_at")
        or column.endswith("_count")
        or column.endswith("_generation")
        or column.endswith("_id")
        or column.endswith("_kind")
        or column.endswith("_role")
        or column.endswith("_state")
        or column.endswith("_status")
        or column.endswith("_version")
    )


def _row_value(row: Any, column: str, index: int) -> Any:
    value = row[column] if isinstance(row, Mapping) else row[index]
    return _json_scalar(value)


@dataclass(frozen=True)
class MigrationTraffic:
    message_id: str
    source: str
    target_role: str
    state: str
    work_item_id: str | None


@dataclass(frozen=True)
class MigrationGuardResult:
    allowed: bool
    unrelated: tuple[MigrationTraffic, ...]
    quantauma_nonterminal: tuple[MigrationTraffic, ...]
    reason: str


def evaluate_migration_guard(
    *,
    agentic_mesh_messages: Iterable[MigrationTraffic],
    quantauma_messages: Iterable[MigrationTraffic],
    expected_work_item_id: str,
    cutover: bool = False,
) -> MigrationGuardResult:
    am_nonterminal = tuple(item for item in agentic_mesh_messages if item.state not in _TERMINAL_MESSAGE_STATES)
    q_nonterminal = tuple(item for item in quantauma_messages if item.state not in _TERMINAL_MESSAGE_STATES)
    unrelated = tuple(
        item
        for item in am_nonterminal
        if cutover or item.work_item_id != expected_work_item_id
    )
    if unrelated:
        reason = "agentic_mesh_unrelated_nonterminal_traffic"
    elif q_nonterminal:
        reason = "quantauma_nonterminal_traffic"
    else:
        reason = "migration_exclusive"
    return MigrationGuardResult(
        allowed=not unrelated and not q_nonterminal,
        unrelated=unrelated,
        quantauma_nonterminal=q_nonterminal,
        reason=reason,
    )


@dataclass(frozen=True)
class DisposableApplyResult:
    completed_steps: tuple[str, ...]
    changed: int
    duplicate_noop: int


def apply_disposable_role_metadata(
    connection: Any,
    *,
    project_id: str,
    sources: Sequence[SourceRoleIdentity],
    fleet_id: str = CANONICAL_FLEET_ID,
    control_schema: bool,
    permit: str,
    stop_after_step: str | None = None,
) -> DisposableApplyResult:
    schema = _require_disposable_connection(connection, permit=permit)
    completed: list[str] = []
    changed = 0
    duplicate = 0
    with connection.transaction():
        for statement in _role_instance_metadata_ddl():
            connection.execute(statement)
    completed.append("schema")
    if stop_after_step == "schema":
        raise InterruptedError("synthetic interruption after schema")
    with connection.transaction():
        for source in sources:
            expected = f"{source.project_id}.{source.role_id}.{source.ordinal}"
            if source.project_id != project_id or source.role_instance_id != expected:
                raise SharedFleetConflict("source identity is not a recognized project assignment")
            fleet_instance_id = stable_fleet_instance_id(
                fleet_id=fleet_id,
                role_id=source.role_id,
                ordinal=source.ordinal,
            )
            row = connection.execute(
                """
                SELECT identity_kind, fleet_instance_id, assigned_project_id
                FROM role_instances WHERE role_instance_id=?
                """,
                (source.role_instance_id,),
            ).fetchone()
            if row is None:
                raise SharedFleetConflict(f"missing source role instance: {source.role_instance_id}")
            if (
                row["identity_kind"] == "project_assignment"
                and row["fleet_instance_id"] == fleet_instance_id
                and row["assigned_project_id"] == project_id
            ):
                duplicate += 1
                continue
            if row["fleet_instance_id"] not in {None, fleet_instance_id}:
                raise SharedFleetConflict("source assignment has divergent fleet metadata")
            result = connection.execute(
                """
                UPDATE role_instances
                SET identity_kind='project_assignment', fleet_instance_id=?, assigned_project_id=?
                WHERE role_instance_id=?
                  AND identity_kind IN ('legacy_project_instance', 'project_assignment')
                  AND (fleet_instance_id IS NULL OR fleet_instance_id=?)
                  AND (assigned_project_id IS NULL OR assigned_project_id=?)
                """,
                (fleet_instance_id, project_id, source.role_instance_id, fleet_instance_id, project_id),
            )
            if result.rowcount != 1:
                raise SharedFleetConflict("assignment update did not affect exactly one row")
            changed += 1
    completed.append("assignments")
    if stop_after_step == "assignments":
        raise InterruptedError("synthetic interruption after assignments")
    if control_schema:
        with connection.transaction():
            for source in sources:
                fleet_instance_id = stable_fleet_instance_id(
                    fleet_id=fleet_id,
                    role_id=source.role_id,
                    ordinal=source.ordinal,
                )
                target = connection.execute(
                    """
                    SELECT identity_kind, fleet_instance_id, assigned_project_id
                    FROM role_instances WHERE role_instance_id=?
                    """,
                    (fleet_instance_id,),
                ).fetchone()
                if target is not None:
                    if target["identity_kind"] != "fleet_instance" or target["fleet_instance_id"] != fleet_instance_id:
                        raise SharedFleetConflict("stable fleet target identity diverges")
                    duplicate += 1
                    continue
                source_row = connection.execute(
                    "SELECT * FROM role_instances WHERE role_instance_id=?",
                    (source.role_instance_id,),
                ).fetchone()
                connection.execute(
                    """
                    INSERT INTO role_instances(
                      role_instance_id, role_id, display_name, service_name, state,
                      authority, codex_endpoint, active_thread_id, active_turn_id,
                      inbox_depth, memory_version, updated_at, identity_kind,
                      fleet_instance_id, assigned_project_id, bound_project_id,
                      binding_generation, binding_state
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        fleet_instance_id,
                        source.role_id,
                        source_row["display_name"],
                        stable_fleet_service_name(fleet_id=fleet_id, role_id=source.role_id, ordinal=source.ordinal),
                        "ready",
                        source_row["authority"],
                        _stable_codex_endpoint(
                            source_endpoint=str(source_row["codex_endpoint"]),
                            service_name=stable_fleet_service_name(
                                fleet_id=fleet_id,
                                role_id=source.role_id,
                                ordinal=source.ordinal,
                            ),
                        ),
                        None,
                        None,
                        0,
                        0,
                        source_row["updated_at"],
                        "fleet_instance",
                        fleet_instance_id,
                        None,
                        None,
                        0,
                        "unbound",
                    ),
                )
                changed += 1
        completed.append("physical_identity")
        if stop_after_step == "physical_identity":
            raise InterruptedError("synthetic interruption after physical identity")
    return DisposableApplyResult(tuple(completed), changed, duplicate)


def rollback_disposable_role_metadata(connection: Any, *, permit: str) -> int:
    _require_disposable_connection(connection, permit=permit)
    changed = 0
    with connection.transaction():
        deleted = connection.execute("DELETE FROM role_instances WHERE identity_kind='fleet_instance'")
        changed += max(deleted.rowcount, 0)
        reset = connection.execute(
            """
            UPDATE role_instances
            SET identity_kind='legacy_project_instance', fleet_instance_id=NULL,
                assigned_project_id=NULL, bound_project_id=NULL,
                binding_generation=0, binding_state='unbound'
            WHERE identity_kind='project_assignment'
            """
        )
        changed += max(reset.rowcount, 0)
    return changed


def project_filtered_dashboard(
    *,
    fleet_rows: Sequence[Mapping[str, Any]],
    activity_rows: Sequence[Mapping[str, Any]],
    project_id: str | None = None,
) -> dict[str, Any]:
    fleet: dict[str, dict[str, Any]] = {}
    for row in fleet_rows:
        if row.get("identity_kind") != "fleet_instance":
            continue
        fleet_instance_id = str(row.get("fleet_instance_id") or row.get("role_instance_id") or "")
        if not fleet_instance_id:
            raise SharedFleetConflict("fleet dashboard row lacks a stable identity")
        projected = {
            "active_thread_id": row.get("active_thread_id"),
            "authority": row.get("authority"),
            "codex_endpoint": row.get("codex_endpoint"),
            "current_message": row.get("current_message"),
            "display_name": row.get("display_name"),
            "effective_state": row.get("effective_state"),
            "fleet_instance_id": fleet_instance_id,
            "health": row.get("health"),
            "memory_count": row.get("memory_count"),
            "queued_messages": row.get("queued_messages"),
            "role_id": row.get("role_id"),
            "role_instance_id": fleet_instance_id,
            "state": row.get("state"),
        }
        prior = fleet.get(fleet_instance_id)
        if prior is not None and prior != projected:
            raise SharedFleetConflict("duplicate fleet dashboard identity diverges")
        fleet[fleet_instance_id] = projected
    activities = [
        dict(row)
        for row in activity_rows
        if project_id is None or row.get("project_id") == project_id
    ]
    return {
        "activity": sorted(
            activities,
            key=lambda item: (
                str(item.get("project_id") or ""),
                str(item.get("fleet_instance_id") or ""),
                str(item.get("activity_id") or ""),
            ),
        ),
        "fleet": [fleet[key] for key in sorted(fleet)],
        "project_filter": project_id,
    }


def _validate_fleet_and_role(*, fleet_id: str, role_id: str, ordinal: int) -> None:
    if fleet_id != CANONICAL_FLEET_ID:
        raise ValueError(f"Stage 1 stable fleet_id must be {CANONICAL_FLEET_ID!r}")
    if not _ID_PATTERN.fullmatch(role_id):
        raise ValueError(f"invalid canonical role_id: {role_id!r}")
    if ordinal < 1:
        raise ValueError("fleet ordinal must be positive")


def _bound_context(binding: FleetBinding, assignment: V4ProjectAssignmentConfig) -> BoundProjectContext:
    return BoundProjectContext(
        fleet_instance_id=binding.fleet_instance_id,
        project_id=assignment.project_id,
        generation=binding.generation,
        database_schema=assignment.database_schema,
        database_credential_ref=assignment.database_credential_ref,
        document_root=assignment.document_root,
        project_root=assignment.project_root,
        workspace_root=assignment.workspace_root,
        repository_roots=assignment.repository_roots,
        codex_home=assignment.codex_home,
    )


def _stable_codex_endpoint(*, source_endpoint: str, service_name: str) -> str:
    if ":" not in source_endpoint:
        raise SharedFleetConflict("source Codex endpoint has no port")
    return f"ws://{service_name}:{source_endpoint.rsplit(':', 1)[-1]}"


def _require_disposable_connection(connection: Any, *, permit: str) -> str:
    if permit != STAGE1_DISPOSABLE_PERMIT:
        raise PermissionError("Stage 1 metadata apply is restricted to disposable tests")
    row = connection.execute("SELECT current_schema() AS schema").fetchone()
    schema = str(row["schema"])
    if not (schema.startswith("test_") or schema.startswith("shared_fleet_test_")):
        raise PermissionError(f"refusing Stage 1 metadata apply outside a disposable schema: {schema}")
    return schema


def _role_instance_metadata_ddl() -> tuple[str, ...]:
    return (
        "ALTER TABLE role_instances ADD COLUMN IF NOT EXISTS identity_kind TEXT NOT NULL DEFAULT 'legacy_project_instance' CHECK (identity_kind IN ('legacy_project_instance', 'project_assignment', 'fleet_instance'))",
        "ALTER TABLE role_instances ADD COLUMN IF NOT EXISTS fleet_instance_id TEXT",
        "ALTER TABLE role_instances ADD COLUMN IF NOT EXISTS assigned_project_id TEXT",
        "ALTER TABLE role_instances ADD COLUMN IF NOT EXISTS bound_project_id TEXT",
        "ALTER TABLE role_instances ADD COLUMN IF NOT EXISTS binding_generation BIGINT NOT NULL DEFAULT 0",
        "ALTER TABLE role_instances ADD COLUMN IF NOT EXISTS binding_state TEXT NOT NULL DEFAULT 'unbound' CHECK (binding_state IN ('unbound', 'binding', 'bound', 'draining'))",
        "CREATE INDEX IF NOT EXISTS idx_role_instances_fleet_instance ON role_instances(fleet_instance_id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_role_instances_fleet_project_assignment ON role_instances(fleet_instance_id, assigned_project_id) WHERE identity_kind='project_assignment'",
    )
