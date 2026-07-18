from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Mapping

import psycopg
from psycopg.types.json import Jsonb

from agentic_mesh_v5.database import DatabaseConfigurationError
from agentic_mesh_v5.database import DatabaseError
from agentic_mesh_v5.database import SCHEMA
from agentic_mesh_v5.flow_definition import FlowDefinition
from agentic_mesh_v5.flow_definition import load_flow
from agentic_mesh_v5.package_resolver import PackageReference
from agentic_mesh_v5.package_resolver import PackageResolutionError
from agentic_mesh_v5.package_resolver import resolve_packages
from agentic_mesh_v5.project_manifest import ProjectManifest
from agentic_mesh_v5.prompt_renderer import PromptRenderError
from agentic_mesh_v5.prompt_renderer import render_role_state_prompt
from agentic_mesh_v5.tool_profiles import ToolProfileError
from agentic_mesh_v5.tool_profiles import ToolProfileRegistry


_ID = re.compile(r"^[a-z][a-z0-9-]{0,127}$")
_EXTERNAL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


class RolePackError(DatabaseError):
    pass


class RolePackConflict(RolePackError):
    pass


@dataclass(frozen=True, slots=True)
class RoleBinding:
    project_id: str
    role_id: str
    manifest_digest: str
    role_reference: str
    role_digest: str
    tool_profile_reference: str
    tool_profile_digest: str
    tool_profile_id: str
    flow_reference: str
    flow_digest: str
    prompt_configuration_digest: str
    role_class: str
    memory_scope: str
    collaboration_identity: str
    minimum_instances: int
    maximum_instances: int


@dataclass(frozen=True, slots=True)
class RolePackActivation:
    project_id: str
    manifest_digest: str
    flow_reference: str
    flow_digest: str
    roles: tuple[RoleBinding, ...]


@dataclass(frozen=True, slots=True)
class _PreparedRole:
    binding: RoleBinding
    snapshot: Mapping[str, object]


class RolePackActivator:
    def __init__(self, database_url: str, configuration_root: Path) -> None:
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise DatabaseConfigurationError("the V5 database URL must use Postgres")
        self._database_url = database_url
        self._configuration_root = Path(configuration_root)
        self._profiles = ToolProfileRegistry(self._configuration_root)

    def activate(
        self,
        manifest: ProjectManifest,
        *,
        flow_reference: str,
        actor_id: str,
    ) -> RolePackActivation:
        if not isinstance(manifest, ProjectManifest):
            raise ValueError("validated project manifest is required")
        actor_id = _external_id(actor_id, "actor_id")
        try:
            parsed_flow = PackageReference.parse(flow_reference)
            if parsed_flow.kind != "flow":
                raise RolePackConflict("flow_reference must select a flow package")
            flow_resolved = resolve_packages(
                self._configuration_root, [flow_reference]
            )
            flow = load_flow(flow_resolved)
            raw_roles = manifest.snapshot.get("roles")
            if not isinstance(raw_roles, Mapping) or not raw_roles:
                raise RolePackConflict("project manifest has no roles")
            prepared = tuple(
                self._prepare_role(
                    manifest,
                    role_id,
                    value,
                    flow_reference,
                    flow,
                )
                for role_id, value in sorted(raw_roles.items())
            )
            configured = {item.binding.role_id for item in prepared}
            missing = sorted(_flow_roles(flow) - configured)
            if missing:
                raise RolePackConflict(f"flow roles are not configured: {missing}")
            for item in prepared:
                referenced = set(item.snapshot["consults"]) | set(
                    item.snapshot["handoff_targets"]
                )
                unknown = sorted(referenced - configured)
                if unknown:
                    raise RolePackConflict(
                        f"role {item.binding.role_id} references unknown roles: {unknown}"
                    )
            return self._activate(manifest, flow, prepared, actor_id)
        except RolePackError:
            raise
        except (
            PackageResolutionError,
            PromptRenderError,
            ToolProfileError,
            ValueError,
        ) as exc:
            raise RolePackConflict(str(exc)) from exc
        except Exception as exc:
            raise RolePackError("role-pack activation failed") from exc

    def get(self, project_id: str) -> RolePackActivation:
        project_id = _identifier(project_id, "project_id")
        try:
            with psycopg.connect(self._database_url, autocommit=True) as connection:
                rows = connection.execute(
                    f"""
                    SELECT project_id, role_id, manifest_digest, role_reference,
                           role_digest, tool_profile_reference, tool_profile_digest,
                           tool_profile_id, flow_reference, flow_digest,
                           prompt_configuration_digest, role_class, memory_scope,
                           collaboration_identity, minimum_instances,
                           maximum_instances
                    FROM {SCHEMA}.role_bindings
                    WHERE project_id = %s ORDER BY role_id
                    """,
                    (project_id,),
                ).fetchall()
            if not rows:
                raise RolePackConflict("project role pack is not active")
            bindings = tuple(RoleBinding(*row) for row in rows)
            manifests = {item.manifest_digest for item in bindings}
            flows = {
                (item.flow_reference, item.flow_digest) for item in bindings
            }
            if len(manifests) != 1 or len(flows) != 1:
                raise RolePackConflict("project role bindings disagree")
            flow_reference, flow_digest = next(iter(flows))
            return RolePackActivation(
                project_id,
                next(iter(manifests)),
                flow_reference,
                flow_digest,
                bindings,
            )
        except RolePackError:
            raise
        except Exception as exc:
            raise RolePackError("role-pack read failed") from exc

    def _prepare_role(
        self,
        manifest: ProjectManifest,
        role_id: object,
        value: object,
        flow_reference: str,
        flow: FlowDefinition,
    ) -> _PreparedRole:
        role_id = _identifier(role_id, "manifest role_id")
        if not isinstance(value, Mapping):
            raise RolePackConflict(f"manifest role {role_id} is invalid")
        role_reference = _string(value.get("package"), "role package")
        profile_reference = _string(value.get("tool_profile"), "tool profile")
        instances = value.get("instances")
        if not isinstance(instances, Mapping):
            raise RolePackConflict(f"role {role_id} instances are invalid")
        minimum = _integer(instances.get("minimum"), "minimum instances", 0, 256)
        maximum = _integer(instances.get("maximum"), "maximum instances", 1, 256)
        if minimum > maximum:
            raise RolePackConflict(f"role {role_id} instance bounds are invalid")
        if role_id == "project-manager" and minimum < 1:
            raise RolePackConflict("project-manager requires one warm instance")

        parsed = PackageReference.parse(role_reference)
        if parsed.kind != "role" or parsed.package_id != role_id:
            raise RolePackConflict(f"role package identity disagrees for {role_id}")
        resolved = resolve_packages(self._configuration_root, [role_reference])
        if "role" not in resolved.settings:
            raise RolePackConflict(f"role package {role_id} has no role settings")
        snapshot = _role_snapshot(resolved.settings["role"], role_id)
        profile = self._profiles.load_for_launch(
            profile_reference,
            launcher="control-plane",
            via_normal_routing=True,
        )
        prompt = render_role_state_prompt(
            self._configuration_root,
            role_reference,
            flow_reference,
            flow.entry_state,
        )
        return _PreparedRole(
            RoleBinding(
                manifest.project_id,
                role_id,
                manifest.digest,
                role_reference,
                resolved.digest,
                profile_reference,
                profile.configuration_digest,
                profile.profile_id,
                flow_reference,
                flow.digest,
                prompt.digest,
                str(snapshot["role_class"]),
                str(snapshot["memory_scope"]),
                role_id,
                minimum,
                maximum,
            ),
            snapshot,
        )

    def _activate(
        self,
        manifest: ProjectManifest,
        flow: FlowDefinition,
        prepared: tuple[_PreparedRole, ...],
        actor_id: str,
    ) -> RolePackActivation:
        try:
            with psycopg.connect(self._database_url, autocommit=True) as connection:
                with connection.transaction():
                    project = connection.execute(
                        f"SELECT project_id FROM {SCHEMA}.projects "
                        "WHERE project_id = %s FOR UPDATE",
                        (manifest.project_id,),
                    ).fetchone()
                    if project is None:
                        raise RolePackConflict("project is not registered")
                    active = connection.execute(
                        f"SELECT manifest_digest FROM {SCHEMA}.project_manifest_active "
                        "WHERE project_id = %s",
                        (manifest.project_id,),
                    ).fetchone()
                    if active != (manifest.digest,):
                        raise RolePackConflict("project manifest is not active")
                    selected = [item.binding.role_id for item in prepared]
                    connection.execute(
                        f"UPDATE {SCHEMA}.roles SET status = 'inactive' "
                        "WHERE project_id = %s AND NOT (role_id = ANY(%s))",
                        (manifest.project_id, selected),
                    )
                    connection.execute(
                        f"UPDATE {SCHEMA}.role_queues SET paused = true "
                        "WHERE project_id = %s AND NOT (role_id = ANY(%s))",
                        (manifest.project_id, selected),
                    )
                    for item in prepared:
                        self._upsert(connection, item, actor_id)
                    connection.execute(
                        f"""
                        INSERT INTO {SCHEMA}.audit_records
                            (scope, project_id, actor_id, action, object_type,
                             object_id, details)
                        VALUES ('project', %s, %s, 'role_pack.activated',
                                'role-pack', %s, %s)
                        """,
                        (
                            manifest.project_id,
                            actor_id,
                            manifest.digest,
                            Jsonb(
                                {
                                    "flow_digest": flow.digest,
                                    "role_count": len(prepared),
                                }
                            ),
                        ),
                    )
            return RolePackActivation(
                manifest.project_id,
                manifest.digest,
                prepared[0].binding.flow_reference,
                flow.digest,
                tuple(item.binding for item in prepared),
            )
        except RolePackError:
            raise
        except Exception as exc:
            raise RolePackError("role-pack persistence failed") from exc

    @staticmethod
    def _upsert(connection, item: _PreparedRole, actor_id: str) -> None:
        binding = item.binding
        connection.execute(
            f"""
            INSERT INTO {SCHEMA}.roles
                (project_id, role_id, template_id, package_digest, status)
            VALUES (%s, %s, %s, %s, 'active')
            ON CONFLICT (project_id, role_id) DO UPDATE
            SET template_id = EXCLUDED.template_id,
                package_digest = EXCLUDED.package_digest, status = 'active'
            """,
            (
                binding.project_id,
                binding.role_id,
                binding.role_id,
                binding.role_digest,
            ),
        )
        connection.execute(
            f"""
            INSERT INTO {SCHEMA}.role_bindings
                (project_id, role_id, manifest_digest, role_reference, role_digest,
                 role_snapshot, tool_profile_reference, tool_profile_digest,
                 tool_profile_id, flow_reference, flow_digest,
                 prompt_configuration_digest, role_class, memory_scope,
                 collaboration_identity, minimum_instances, maximum_instances,
                 activated_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s)
            ON CONFLICT (project_id, role_id) DO UPDATE
            SET manifest_digest = EXCLUDED.manifest_digest,
                role_reference = EXCLUDED.role_reference,
                role_digest = EXCLUDED.role_digest,
                role_snapshot = EXCLUDED.role_snapshot,
                tool_profile_reference = EXCLUDED.tool_profile_reference,
                tool_profile_digest = EXCLUDED.tool_profile_digest,
                tool_profile_id = EXCLUDED.tool_profile_id,
                flow_reference = EXCLUDED.flow_reference,
                flow_digest = EXCLUDED.flow_digest,
                prompt_configuration_digest = EXCLUDED.prompt_configuration_digest,
                role_class = EXCLUDED.role_class,
                memory_scope = EXCLUDED.memory_scope,
                collaboration_identity = EXCLUDED.collaboration_identity,
                minimum_instances = EXCLUDED.minimum_instances,
                maximum_instances = EXCLUDED.maximum_instances,
                activated_by = EXCLUDED.activated_by,
                activated_at = clock_timestamp()
            """,
            (
                binding.project_id,
                binding.role_id,
                binding.manifest_digest,
                binding.role_reference,
                binding.role_digest,
                Jsonb(dict(item.snapshot)),
                binding.tool_profile_reference,
                binding.tool_profile_digest,
                binding.tool_profile_id,
                binding.flow_reference,
                binding.flow_digest,
                binding.prompt_configuration_digest,
                binding.role_class,
                binding.memory_scope,
                binding.collaboration_identity,
                binding.minimum_instances,
                binding.maximum_instances,
                actor_id,
            ),
        )
        connection.execute(
            f"""
            INSERT INTO {SCHEMA}.role_queues(project_id, queue_id, role_id, paused)
            VALUES (%s, %s, %s, false)
            ON CONFLICT (project_id, queue_id) DO UPDATE
            SET role_id = EXCLUDED.role_id, capability = NULL, paused = false
            """,
            (binding.project_id, binding.role_id, binding.role_id),
        )
        for index in range(1, binding.maximum_instances + 1):
            instance_id = f"{binding.project_id}.{binding.role_id}.{index}"
            connection.execute(
                f"""
                INSERT INTO {SCHEMA}.role_instances
                    (project_id, instance_id, role_id, status, metadata)
                VALUES (%s, %s, %s, 'configured', %s)
                ON CONFLICT (project_id, instance_id) DO UPDATE
                SET role_id = EXCLUDED.role_id, metadata = EXCLUDED.metadata
                """,
                (
                    binding.project_id,
                    instance_id,
                    binding.role_id,
                    Jsonb({"role_binding": binding.role_id}),
                ),
            )
        connection.execute(
            f"""
            INSERT INTO {SCHEMA}.role_scaling_policies
                (project_id, role_id, min_warm_instances, max_instances,
                 scale_after_seconds, idle_grace_seconds, hibernation_enabled)
            VALUES (%s, %s, %s, %s, 60, 300, %s)
            ON CONFLICT (project_id, role_id) DO UPDATE
            SET min_warm_instances = EXCLUDED.min_warm_instances,
                max_instances = EXCLUDED.max_instances,
                scale_after_seconds = EXCLUDED.scale_after_seconds,
                idle_grace_seconds = EXCLUDED.idle_grace_seconds,
                hibernation_enabled = EXCLUDED.hibernation_enabled,
                updated_at = clock_timestamp()
            """,
            (
                binding.project_id,
                binding.role_id,
                binding.minimum_instances,
                binding.maximum_instances,
                binding.role_id != "project-manager",
            ),
        )


def _role_snapshot(value: object, expected_id: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise RolePackConflict("role content is invalid")
    required = {
        "schema_version",
        "role_id",
        "display_name",
        "role_class",
        "purpose",
        "accountabilities",
        "decision_rights",
        "consults",
        "handoff_targets",
        "memory_scope",
        "instructions",
        "documentation",
    }
    if set(value) != required or value.get("schema_version") != 1:
        raise RolePackConflict(f"role {expected_id} fields are invalid")
    if value.get("role_id") != expected_id:
        raise RolePackConflict(f"role content identity disagrees for {expected_id}")
    if value.get("memory_scope") != "project-role":
        raise RolePackConflict(f"role {expected_id} memory scope is invalid")
    for key in ("consults", "handoff_targets"):
        values = value.get(key)
        if not isinstance(values, list) or not all(
            isinstance(item, str) and _ID.fullmatch(item) for item in values
        ):
            raise RolePackConflict(f"role {expected_id} {key} are invalid")
    _string(value.get("role_class"), "role_class")
    return json.loads(json.dumps(value, sort_keys=True))


def _flow_roles(flow: FlowDefinition) -> set[str]:
    roles = {flow.leader_role}
    for state in flow.states.values():
        roles.add(state.owner_role)
        roles.update(action.role_id for action in state.consults)
        roles.update(action.role_id for action in state.informs)
        roles.update(
            action.role_id for action in state.gates if action.role_id != "sponsor"
        )
        roles.update(route.target_role for route in state.routes)
    return roles


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise RolePackConflict(f"{label} is invalid")
    return value


def _external_id(value: object, label: str) -> str:
    if not isinstance(value, str) or _EXTERNAL_ID.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RolePackConflict(f"{label} is invalid")
    return value.strip()


def _integer(value: object, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or value < minimum or value > maximum:
        raise RolePackConflict(f"{label} is invalid")
    return value
