from __future__ import annotations

from dataclasses import asdict, dataclass
import os
from pathlib import Path
import re
import subprocess
from typing import Mapping, Sequence

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from agentic_mesh_v5.config_activation import ConfigActivationStore
from agentic_mesh_v5.database import DatabaseConfigurationError, DatabaseError, SCHEMA
from agentic_mesh_v5.git_worktrees import GitWorktreeCoordinator, WorkItemWorkspace
from agentic_mesh_v5.lifecycle import LifecycleStore
from agentic_mesh_v5.package_resolver import PackageResolutionError, resolve_packages
from agentic_mesh_v5.project_manifest import (
    ProjectManifest,
    ProjectManifestActivation,
    ProjectManifestStore,
)
from agentic_mesh_v5.role_pack import RolePackActivation, RolePackActivator


_ID = re.compile(r"^[a-z][a-z0-9-]{0,127}$")
_PROJECT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_IMAGE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
_ALLOWED_CONFIG_STATE = ("activation/", "releases/", "state/")


class ProjectRegistrationError(DatabaseError):
    pass


class ProjectRegistrationConflict(ProjectRegistrationError):
    pass


@dataclass(frozen=True, slots=True)
class ProjectRuntimeBoundary:
    project_id: str
    manifest_digest: str
    configuration_revision: str
    configuration_root: str
    running_image_ref: str
    running_install_root: str
    runtime_state_root: str
    workspace_root: str
    deployment_adapter: str
    registered_by: str
    registered_at: str
    version: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ProjectRegistration:
    manifest: ProjectManifestActivation
    roles: RolePackActivation
    boundary: ProjectRuntimeBoundary

    def to_dict(self) -> dict[str, object]:
        return {
            "manifest": self.manifest.to_dict(),
            "roles": {
                "project_id": self.roles.project_id,
                "manifest_digest": self.roles.manifest_digest,
                "flow_reference": self.roles.flow_reference,
                "flow_digest": self.roles.flow_digest,
                "role_count": len(self.roles.roles),
            },
            "boundary": self.boundary.to_dict(),
        }


class ProjectRegistrationCoordinator:
    """Compose ordinary project services without a self-hosting special case."""

    def __init__(self, database_url: str, *, configuration_root: Path) -> None:
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise DatabaseConfigurationError("the V5 database URL must use Postgres")
        self._database_url = database_url
        self._configuration_root = _existing_root(
            configuration_root, "configuration_root"
        )

    def register(
        self,
        manifest: ProjectManifest,
        *,
        source_revision: str,
        configuration_revision: str,
        flow_reference: str,
        sponsor_ids: Sequence[str],
        running_image_ref: str,
        running_install_root: Path,
        runtime_state_root: Path,
        workspace_root: Path,
        deployment_adapter: str,
        actor_id: str,
    ) -> ProjectRegistration:
        if not isinstance(manifest, ProjectManifest):
            raise ValueError("validated project manifest is required")
        configuration_revision = _commit(configuration_revision)
        actor_id = _text(actor_id, "actor_id")
        deployment_adapter = _identifier(deployment_adapter, "deployment_adapter")
        running_image_ref = _image(running_image_ref)
        roots = _runtime_roots(
            self._configuration_root,
            running_install_root,
            runtime_state_root,
            workspace_root,
        )
        boundary_values = {
            "configuration_revision": configuration_revision,
            "running_image_ref": running_image_ref,
            "configuration_root": roots[0],
            "running_install_root": roots[1],
            "runtime_state_root": roots[2],
            "workspace_root": roots[3],
            "deployment_adapter": deployment_adapter,
        }
        source_revision = _commit(source_revision)
        with psycopg.connect(self._database_url, autocommit=True) as lock:
            lock.execute(
                "SELECT pg_advisory_lock(hashtextextended(%s, 0))",
                ("project-registration",),
            )
            try:
                self._verify_configuration(configuration_revision, manifest)
                self._preflight_boundary(manifest, boundary_values)
                self._ensure_project(manifest, sponsor_ids)
                activation = ProjectManifestStore(self._database_url).activate(
                    manifest,
                    source_revision=source_revision,
                    actor_id=actor_id,
                )
                roles = RolePackActivator(
                    self._database_url, self._configuration_root
                ).activate(
                    manifest,
                    flow_reference=flow_reference,
                    actor_id=actor_id,
                )
                boundary = self._register_boundary(
                    manifest,
                    actor_id=actor_id,
                    values=boundary_values,
                )
            finally:
                lock.execute(
                    "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
                    ("project-registration",),
                )
        return ProjectRegistration(activation, roles, boundary)

    def get(self, project_id: str) -> ProjectRuntimeBoundary | None:
        project_id = _project_identifier(project_id)
        try:
            with psycopg.connect(
                self._database_url, autocommit=True, row_factory=dict_row
            ) as connection:
                row = connection.execute(
                    f"""
                    SELECT project_id, manifest_digest, configuration_revision,
                           configuration_root, running_image_ref,
                           running_install_root, runtime_state_root,
                           workspace_root, deployment_adapter, registered_by,
                           registered_at::text, version
                    FROM {SCHEMA}.project_runtime_boundaries
                    WHERE project_id = %s
                    """,
                    (project_id,),
                ).fetchone()
            return None if row is None else ProjectRuntimeBoundary(**row)
        except ProjectRegistrationError:
            raise
        except Exception as exc:
            raise ProjectRegistrationError("project registration read failed") from exc

    def prepare_workspace(
        self,
        *,
        project_id: str,
        work_item_id: str,
        actor_id: str,
        source_repositories: Mapping[str, Path],
    ) -> WorkItemWorkspace:
        boundary = self.get(project_id)
        if boundary is None:
            raise ProjectRegistrationConflict(
                "project runtime boundary is not registered"
            )
        protected = self._protected_roots()
        for source in source_repositories.values():
            selected = _existing_root(source, "source repository")
            if any(_overlaps(selected, root) for root in protected):
                raise ProjectRegistrationConflict(
                    "source repository overlaps a protected runtime boundary"
                )
        return GitWorktreeCoordinator(
            self._database_url, workspace_root=Path(boundary.workspace_root)
        ).prepare(
            project_id=project_id,
            work_item_id=work_item_id,
            actor_id=actor_id,
            source_repositories=source_repositories,
        )

    def _protected_roots(self) -> tuple[Path, ...]:
        try:
            with psycopg.connect(
                self._database_url, autocommit=True, row_factory=dict_row
            ) as connection:
                rows = self._boundary_rows(connection)
            return tuple(
                Path(str(row[key]))
                for row in rows
                for key in (
                    "configuration_root",
                    "running_install_root",
                    "runtime_state_root",
                    "workspace_root",
                )
            )
        except ProjectRegistrationError:
            raise
        except Exception as exc:
            raise ProjectRegistrationError(
                "project runtime boundary read failed"
            ) from exc

    def _verify_configuration(
        self, revision: str, manifest: ProjectManifest
    ) -> None:
        head = _git(self._configuration_root, "rev-parse", "HEAD").lower()
        if head != revision:
            raise ProjectRegistrationConflict(
                "configuration checkout does not match its pinned revision"
            )
        for line in _git(
            self._configuration_root, "status", "--porcelain=v1"
        ).splitlines():
            relative = line[3:].replace("\\", "/")
            if not any(relative.startswith(prefix) for prefix in _ALLOWED_CONFIG_STATE):
                raise ProjectRegistrationConflict(
                    "configuration checkout contains uncommitted package changes"
                )
        packages = manifest.snapshot.get("packages")
        digest = (
            packages.get("organization_release_digest")
            if isinstance(packages, Mapping)
            else None
        )
        state = ConfigActivationStore(self._configuration_root).get_state()
        if state.active_digest != digest:
            raise ProjectRegistrationConflict(
                "manifest organization release is not the active configuration"
            )
        overrides = packages.get("overrides") if isinstance(packages, Mapping) else None
        if not isinstance(overrides, list):
            raise ProjectRegistrationConflict("manifest package overrides are invalid")
        for reference in overrides:
            try:
                resolved = resolve_packages(self._configuration_root, [reference])
            except PackageResolutionError as exc:
                raise ProjectRegistrationConflict(
                    f"project override cannot be resolved: {reference}"
                ) from exc
            if resolved.settings.get("project_id") != manifest.project_id:
                raise ProjectRegistrationConflict(
                    f"project override identity disagrees: {reference}"
                )

    def _ensure_project(
        self, manifest: ProjectManifest, sponsor_ids: Sequence[str]
    ) -> None:
        supplied = tuple(_text(item, "sponsor_id") for item in sponsor_ids)
        if len(supplied) != len(set(supplied)):
            raise ValueError("sponsor_ids must be unique")
        sponsors = tuple(sorted(supplied))
        if not sponsors:
            raise ValueError("at least one sponsor_id is required")
        try:
            with psycopg.connect(self._database_url, autocommit=True) as connection:
                row = connection.execute(
                    f"SELECT display_name FROM {SCHEMA}.projects WHERE project_id = %s",
                    (manifest.project_id,),
                ).fetchone()
                current_sponsors = tuple(
                    item[0]
                    for item in connection.execute(
                        f"SELECT sponsor_id FROM {SCHEMA}.project_sponsors "
                        "WHERE project_id = %s ORDER BY sponsor_id",
                        (manifest.project_id,),
                    ).fetchall()
                )
            if row is None:
                LifecycleStore(self._database_url).create_project(
                    project_id=manifest.project_id,
                    display_name=manifest.display_name,
                    sponsor_ids=sponsors,
                )
            elif row != (manifest.display_name,) or current_sponsors != sponsors:
                raise ProjectRegistrationConflict(
                    "registered project identity or sponsors conflict"
                )
        except ProjectRegistrationError:
            raise
        except Exception as exc:
            raise ProjectRegistrationError(
                "project identity registration failed"
            ) from exc

    def _preflight_boundary(
        self,
        manifest: ProjectManifest,
        values: Mapping[str, object],
    ) -> None:
        requested = {
            "project_id": manifest.project_id,
            "manifest_digest": manifest.digest,
            **values,
        }
        try:
            with psycopg.connect(
                self._database_url, autocommit=True, row_factory=dict_row
            ) as connection:
                rows = self._boundary_rows(connection)
                self._validate_boundary_rows(requested, rows)
        except ProjectRegistrationError:
            raise
        except Exception as exc:
            raise ProjectRegistrationError(
                "project registration preflight failed"
            ) from exc

    def _register_boundary(
        self,
        manifest: ProjectManifest,
        *,
        actor_id: str,
        values: Mapping[str, object],
    ) -> ProjectRuntimeBoundary:
        requested = {
            "project_id": manifest.project_id,
            "manifest_digest": manifest.digest,
            **values,
        }
        try:
            with psycopg.connect(
                self._database_url, autocommit=True, row_factory=dict_row
            ) as connection:
                with connection.transaction():
                    connection.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        ("project-runtime-boundaries",),
                    )
                    rows = self._boundary_rows(connection)
                    existing = self._validate_boundary_rows(requested, rows)
                    if existing is not None:
                        return existing
                    row = connection.execute(
                        f"""
                        INSERT INTO {SCHEMA}.project_runtime_boundaries
                            (project_id, manifest_digest, configuration_revision,
                             configuration_root, running_image_ref,
                             running_install_root, runtime_state_root,
                             workspace_root, deployment_adapter, registered_by)
                        VALUES (%(project_id)s, %(manifest_digest)s,
                                %(configuration_revision)s, %(configuration_root)s,
                                %(running_image_ref)s, %(running_install_root)s,
                                %(runtime_state_root)s, %(workspace_root)s,
                                %(deployment_adapter)s, %(registered_by)s)
                        RETURNING project_id, manifest_digest,
                                  configuration_revision, configuration_root,
                                  running_image_ref, running_install_root,
                                  runtime_state_root, workspace_root,
                                  deployment_adapter, registered_by,
                                  registered_at::text, version
                        """,
                        {**requested, "registered_by": actor_id},
                    ).fetchone()
                    connection.execute(
                        f"""
                        INSERT INTO {SCHEMA}.audit_records
                            (scope, project_id, actor_id, action, object_type,
                             object_id, details)
                        VALUES ('project', %(project_id)s, %(actor_id)s,
                                'project.runtime_registered', 'runtime-boundary',
                                %(manifest_digest)s, %(details)s)
                        """,
                        {
                            **requested,
                            "actor_id": actor_id,
                            "details": Jsonb(
                                {
                                    "configuration_revision": requested[
                                        "configuration_revision"
                                    ],
                                    "running_image_ref": requested["running_image_ref"],
                                    "deployment_adapter": requested[
                                        "deployment_adapter"
                                    ],
                                }
                            ),
                        },
                    )
                    return ProjectRuntimeBoundary(**row)
        except ProjectRegistrationError:
            raise
        except psycopg.errors.UniqueViolation as exc:
            raise ProjectRegistrationConflict(
                "project runtime boundary is already claimed"
            ) from exc
        except Exception as exc:
            raise ProjectRegistrationError("project registration failed") from exc

    @staticmethod
    def _boundary_rows(connection) -> list[Mapping[str, object]]:
        return connection.execute(
            f"""
            SELECT project_id, manifest_digest, configuration_revision,
                   configuration_root, running_image_ref,
                   running_install_root, runtime_state_root, workspace_root,
                   deployment_adapter, registered_by, registered_at::text,
                   version
            FROM {SCHEMA}.project_runtime_boundaries
            """
        ).fetchall()

    @staticmethod
    def _validate_boundary_rows(
        requested: Mapping[str, object], rows: Sequence[Mapping[str, object]]
    ) -> ProjectRuntimeBoundary | None:
        requested_roots = tuple(
            Path(str(requested[key]))
            for key in (
                "running_install_root",
                "runtime_state_root",
                "workspace_root",
            )
        )
        for row in rows:
            if row["project_id"] == requested["project_id"]:
                comparable = dict(row)
                registered_by = str(comparable.pop("registered_by"))
                registered_at = str(comparable.pop("registered_at"))
                version = int(comparable.pop("version"))
                if comparable != requested:
                    raise ProjectRegistrationConflict(
                        "project runtime boundary conflicts with registration"
                    )
                return ProjectRuntimeBoundary(
                    **comparable,
                    registered_by=registered_by,
                    registered_at=registered_at,
                    version=version,
                )
            if any(
                _overlaps(left, Path(str(row[key])))
                for left in requested_roots
                for key in (
                    "running_install_root",
                    "runtime_state_root",
                    "workspace_root",
                )
            ):
                raise ProjectRegistrationConflict(
                    "project runtime boundary overlaps another project"
                )
        return None


def _runtime_roots(
    configuration: Path,
    running_install: Path,
    runtime_state: Path,
    workspace: Path,
) -> tuple[str, str, str, str]:
    roots = (
        configuration,
        _absolute(running_install, "running_install_root"),
        _absolute(runtime_state, "runtime_state_root"),
        _absolute(workspace, "workspace_root"),
    )
    for index, root in enumerate(roots):
        for other in roots[index + 1 :]:
            if _overlaps(root, other):
                raise ProjectRegistrationConflict(
                    "configuration, install, state and workspace roots must be separate"
                )
    return tuple(str(item) for item in roots)


def _existing_root(value: object, field: str) -> Path:
    path = _absolute(value, field)
    if not path.is_dir():
        raise ValueError(f"{field} must be an existing directory")
    return path


def _absolute(value: object, field: str) -> Path:
    if not isinstance(value, Path) or not value.is_absolute():
        raise ValueError(f"{field} must be an absolute path")
    if any(item in str(value) for item in ("\n", "\r", "\x00")):
        raise ValueError(f"{field} contains unsupported characters")
    return value.resolve()


def _overlaps(left: Path, right: Path) -> bool:
    left_value = Path(os.path.normcase(str(left.resolve())))
    right_value = Path(os.path.normcase(str(right.resolve())))
    try:
        left_value.relative_to(right_value)
        return True
    except ValueError:
        try:
            right_value.relative_to(left_value)
            return True
        except ValueError:
            return False


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise ProjectRegistrationConflict(
            f"configuration Git command failed: {arguments[0]}"
        )
    return completed.stdout.strip()


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ValueError(f"{field} is invalid")
    return value


def _project_identifier(value: object) -> str:
    if not isinstance(value, str) or _PROJECT_ID.fullmatch(value) is None:
        raise ValueError("project_id is invalid")
    return value


def _commit(value: object) -> str:
    if not isinstance(value, str) or _COMMIT.fullmatch(value) is None:
        raise ValueError("revision is invalid")
    return value


def _image(value: object) -> str:
    if not isinstance(value, str) or _IMAGE.fullmatch(value) is None:
        raise ValueError("running_image_ref must be digest-pinned")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{field} is invalid")
    return value.strip()
