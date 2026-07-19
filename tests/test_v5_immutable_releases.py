from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
import uuid
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql
import pytest

from agentic_mesh_v5.database import MigrationRunner
from agentic_mesh_v5.immutable_releases import DeploymentObservation
from agentic_mesh_v5.immutable_releases import ImmutableReleaseCoordinator
from agentic_mesh_v5.immutable_releases import UpgradeRequest
from agentic_mesh_v5.immutable_releases import VerifiedImage


PREVIOUS = "ghcr.io/example/mesh@sha256:" + "a" * 64
CANDIDATE = "ghcr.io/example/mesh@sha256:" + "b" * 64
THIRD = "ghcr.io/example/mesh@sha256:" + "c" * 64
SOURCE_REVISION = "1" * 40


@pytest.fixture
def postgres_database() -> str:
    base_url = os.environ.get("AGENTIC_MESH_TEST_DATABASE_URL")
    if not base_url:
        pytest.skip("AGENTIC_MESH_TEST_DATABASE_URL is required for Postgres tests")
    database_name = f"mesh_v5_{uuid.uuid4().hex}"
    with psycopg.connect(base_url, autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name))
        )
    parsed = urlsplit(base_url)
    database_url = urlunsplit(parsed._replace(path=f"/{database_name}"))
    try:
        yield database_url
    finally:
        with psycopg.connect(base_url, autocommit=True) as connection:
            connection.execute(
                """
                SELECT pg_terminate_backend(pid) FROM pg_stat_activity
                WHERE datname = %s AND pid <> pg_backend_pid()
                """,
                (database_name,),
            )
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(
                    sql.Identifier(database_name)
                )
            )


@pytest.fixture
def release_project(postgres_database: str, tmp_path: Path) -> UpgradeRequest:
    assert MigrationRunner(postgres_database).migrate().current_version == 27
    roots = []
    for name in ("configuration", "running", "state", "workspaces", "source"):
        root = tmp_path / name
        root.mkdir()
        roots.append(root)
    manifest_digest = "d" * 64
    with psycopg.connect(postgres_database) as connection:
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.projects(project_id, display_name)
            VALUES ('alpha', 'Alpha')
            """
        )
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.project_manifest_snapshots
                (project_id, manifest_digest, source_revision, source_path,
                 snapshot, registered_by)
            VALUES ('alpha', %s, %s, 'agentic-mesh/project.yaml', '{}', 'pm')
            """,
            (manifest_digest, SOURCE_REVISION),
        )
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.project_manifest_active
                (project_id, manifest_digest, activated_by)
            VALUES ('alpha', %s, 'pm')
            """,
            (manifest_digest,),
        )
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.project_runtime_boundaries
                (project_id, manifest_digest, configuration_revision,
                 configuration_root, running_image_ref, running_install_root,
                 runtime_state_root, workspace_root, deployment_adapter,
                 registered_by)
            VALUES ('alpha', %s, %s, %s, %s, %s, %s, %s, 'compose', 'pm')
            """,
            (
                manifest_digest,
                "e" * 40,
                str(roots[0]),
                PREVIOUS,
                str(roots[1]),
                str(roots[2]),
                str(roots[3]),
            ),
        )
    return UpgradeRequest(
        project_id="alpha",
        operation_id="upgrade-1",
        source_root=roots[4],
        source_revision=SOURCE_REVISION,
        actor_id="release-manager",
    )


class FakeBuilder:
    def __init__(self, image: VerifiedImage | None = None) -> None:
        self.image = image or _candidate(CANDIDATE)
        self.calls: list[tuple[UpgradeRequest, str]] = []
        self.error: Exception | None = None

    def build_and_verify(
        self, request: UpgradeRequest, *, release_id: str
    ) -> VerifiedImage:
        self.calls.append((request, release_id))
        if self.error is not None:
            raise self.error
        return self.image


class SimulatedCrash(BaseException):
    pass


class FakeDeployment:
    def __init__(self) -> None:
        self.image = PREVIOUS
        self.health: dict[str, bool] = {
            PREVIOUS: True,
            CANDIDATE: True,
            THIRD: True,
        }
        self.deploy_calls: list[str] = []
        self.validate_calls: list[Path] = []
        self.fail_deploy: set[str] = set()
        self.crash_after_deploy: set[str] = set()

    def validate(self, *, forbidden_source_root: Path) -> None:
        self.validate_calls.append(forbidden_source_root)

    def observe(self, *, project_id: str) -> DeploymentObservation:
        assert project_id == "alpha"
        return DeploymentObservation(
            image_ref=self.image,
            healthy=self.health.get(self.image, False),
            evidence_ref=f"observe:{self.image[-12:]}",
        )

    def deploy(self, *, project_id: str, image_ref: str) -> None:
        assert project_id == "alpha"
        self.deploy_calls.append(image_ref)
        if image_ref in self.fail_deploy:
            raise RuntimeError(f"deploy failed for {image_ref[-8:]}")
        self.image = image_ref
        if image_ref in self.crash_after_deploy:
            self.crash_after_deploy.remove(image_ref)
            raise SimulatedCrash(image_ref)


def _candidate(
    image_ref: str,
    *,
    minimum: int = 27,
    maximum: int = 27,
) -> VerifiedImage:
    return VerifiedImage(
        image_ref=image_ref,
        minimum_database_version=minimum,
        maximum_database_version=maximum,
        build_evidence_ref="sha256:" + "4" * 64,
        test_evidence_ref="sha256:" + "5" * 64,
    )


def test_verified_upgrade_is_durable_and_exact_replay_has_no_side_effects(
    postgres_database: str, release_project: UpgradeRequest
) -> None:
    builder = FakeBuilder()
    deployment = FakeDeployment()
    coordinator = ImmutableReleaseCoordinator(
        postgres_database, builder=builder, deployment=deployment
    )

    result = coordinator.upgrade(release_project)

    assert result.status == "deployed"
    assert result.previous_image_ref == PREVIOUS
    assert deployment.image == CANDIDATE
    assert deployment.deploy_calls == [CANDIDATE]
    assert len(builder.calls) == 1
    assert coordinator.upgrade(release_project) == result
    assert deployment.deploy_calls == [CANDIDATE]
    assert len(builder.calls) == 1
    with psycopg.connect(postgres_database) as connection:
        boundary = connection.execute(
            """
            SELECT running_image_ref, version
            FROM agentic_mesh_v5.project_runtime_boundaries
            WHERE project_id='alpha'
            """
        ).fetchone()
        release = connection.execute(
            """
            SELECT status, source_revision, minimum_database_version,
                   maximum_database_version
            FROM agentic_mesh_v5.project_runtime_releases
            WHERE project_id='alpha'
            """
        ).fetchone()
        audit = connection.execute(
            """
            SELECT count(*) FROM agentic_mesh_v5.audit_records
            WHERE project_id='alpha' AND action='project.runtime_deployed'
            """
        ).fetchone()[0]
    assert boundary == (CANDIDATE, 2)
    assert release == ("deployed", SOURCE_REVISION, 27, 27)
    assert audit == 1

    with psycopg.connect(postgres_database) as connection:
        release_id = connection.execute(
            """
            SELECT release_id FROM agentic_mesh_v5.project_runtime_releases
            WHERE project_id='alpha'
            """
        ).fetchone()[0]
        with pytest.raises(psycopg.errors.CheckViolation):
            with connection.transaction():
                connection.execute(
                    """
                    INSERT INTO agentic_mesh_v5.project_runtime_deployment_attempts
                        (project_id, operation_id, source_revision,
                         candidate_release_id, status, started_by)
                    VALUES ('alpha', 'invalid-rollback-metadata', %s, %s,
                            'deploying', 'test')
                    """,
                    (SOURCE_REVISION, release_id),
                )


def test_preflight_and_schema_failures_never_touch_the_deployment(
    postgres_database: str, release_project: UpgradeRequest
) -> None:
    deployment = FakeDeployment()
    failed_builder = FakeBuilder()
    failed_builder.error = RuntimeError("tests failed")
    failed = ImmutableReleaseCoordinator(
        postgres_database, builder=failed_builder, deployment=deployment
    ).upgrade(release_project)
    assert failed.status == "rejected"
    assert failed.evidence["stage"] == "preflight"
    assert deployment.deploy_calls == []
    assert deployment.image == PREVIOUS

    incompatible_request = replace(release_project, operation_id="upgrade-2")
    incompatible = ImmutableReleaseCoordinator(
        postgres_database,
        builder=FakeBuilder(_candidate(CANDIDATE, minimum=28, maximum=29)),
        deployment=deployment,
    ).upgrade(incompatible_request)
    assert incompatible.status == "rejected"
    assert "database schema" in incompatible.error
    assert deployment.deploy_calls == []


def test_failed_candidate_health_restores_and_verifies_previous_image(
    postgres_database: str, release_project: UpgradeRequest
) -> None:
    deployment = FakeDeployment()
    deployment.health[CANDIDATE] = False

    result = ImmutableReleaseCoordinator(
        postgres_database, builder=FakeBuilder(), deployment=deployment
    ).upgrade(release_project)

    assert result.status == "rolled_back"
    assert deployment.deploy_calls == [CANDIDATE, PREVIOUS]
    assert deployment.image == PREVIOUS
    with psycopg.connect(postgres_database) as connection:
        assert connection.execute(
            """
            SELECT running_image_ref FROM agentic_mesh_v5.project_runtime_boundaries
            WHERE project_id='alpha'
            """
        ).fetchone()[0] == PREVIOUS


def test_failed_rollback_is_terminal_and_never_claims_the_candidate(
    postgres_database: str, release_project: UpgradeRequest
) -> None:
    deployment = FakeDeployment()
    deployment.health[CANDIDATE] = False
    deployment.fail_deploy.add(PREVIOUS)

    result = ImmutableReleaseCoordinator(
        postgres_database, builder=FakeBuilder(), deployment=deployment
    ).upgrade(release_project)

    assert result.status == "failed"
    assert "deploy failed" in result.error
    assert deployment.image == CANDIDATE
    with psycopg.connect(postgres_database) as connection:
        assert connection.execute(
            """
            SELECT running_image_ref FROM agentic_mesh_v5.project_runtime_boundaries
            WHERE project_id='alpha'
            """
        ).fetchone()[0] == PREVIOUS


def test_restart_finishes_candidate_deployed_before_attempt_checkpoint(
    postgres_database: str, release_project: UpgradeRequest
) -> None:
    deployment = FakeDeployment()
    deployment.crash_after_deploy.add(CANDIDATE)
    builder = FakeBuilder()
    coordinator = ImmutableReleaseCoordinator(
        postgres_database, builder=builder, deployment=deployment
    )

    with pytest.raises(SimulatedCrash):
        coordinator.upgrade(release_project)
    assert coordinator.get("alpha", "upgrade-1").status == "deploying"
    assert deployment.image == CANDIDATE

    resumed = ImmutableReleaseCoordinator(
        postgres_database, builder=builder, deployment=deployment
    ).upgrade(release_project)
    assert resumed.status == "deployed"
    assert len(builder.calls) == 1
    assert deployment.deploy_calls == [CANDIDATE]


def test_restart_finishes_rollback_deployed_before_attempt_checkpoint(
    postgres_database: str, release_project: UpgradeRequest
) -> None:
    deployment = FakeDeployment()
    deployment.health[CANDIDATE] = False
    deployment.crash_after_deploy.add(PREVIOUS)
    builder = FakeBuilder()
    coordinator = ImmutableReleaseCoordinator(
        postgres_database, builder=builder, deployment=deployment
    )

    with pytest.raises(SimulatedCrash):
        coordinator.upgrade(release_project)
    assert coordinator.get("alpha", "upgrade-1").status == "rolling_back"
    assert deployment.image == PREVIOUS

    resumed = ImmutableReleaseCoordinator(
        postgres_database, builder=builder, deployment=deployment
    ).upgrade(release_project)
    assert resumed.status == "rolled_back"
    assert len(builder.calls) == 1
    assert deployment.deploy_calls == [CANDIDATE, PREVIOUS]


def test_observed_deployment_drift_fails_closed(
    postgres_database: str, release_project: UpgradeRequest
) -> None:
    deployment = FakeDeployment()
    deployment.image = THIRD

    result = ImmutableReleaseCoordinator(
        postgres_database, builder=FakeBuilder(), deployment=deployment
    ).upgrade(release_project)

    assert result.status == "rejected"
    assert "differs" in result.error
    assert deployment.deploy_calls == []
